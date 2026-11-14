import asyncio
import base64
import io
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import aiohttp
from PIL import Image as PILImage

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, AtAll, Face, Image, Plain, Poke, Reply

from .image_vision_cache import ImageVisionCache

logger = logging.getLogger("astrbot")


class MediaManager:
    def __init__(
        self,
        config: dict,
        *,
        db_path: str | None = None,
        get_conf=None,
        resolve_vision_llm=None,
    ):
        self.config = config
        self._get_conf = get_conf or (lambda k, d: self.config.get(k, d))
        self._resolve_vision_llm = resolve_vision_llm
        self._image_cache: ImageVisionCache | None = None
        self._warned_essence_requires_json = False
        vision_llm = self.config.get("vision_llm", {})
        try:
            max_concurrency = int(
                (vision_llm or {}).get("max_concurrency", 1)
            )
        except (AttributeError, TypeError, ValueError):
            max_concurrency = 1
        self._vision_max_concurrency = max(1, min(8, max_concurrency))
        self._vision_semaphore = asyncio.Semaphore(self._vision_max_concurrency)
        self._vision_consecutive_failures = 0
        self._vision_circuit_open_until = 0.0
        self._vision_last_circuit_log_at = 0.0
        if db_path:
            try:
                self._image_cache = ImageVisionCache(db_path, self._get_conf)
            except Exception as e:
                logger.error(f"[vision_media] Failed to init image cache: {e}")
                self._image_cache = None

    def close(self):
        if self._image_cache:
            try:
                self._image_cache.close()
            except Exception:
                pass

    def _vision_llm_config(self) -> dict:
        raw = self.config.get("vision_llm", {})
        conf = dict(raw) if isinstance(raw, dict) else {}
        if not callable(self._resolve_vision_llm):
            return conf
        try:
            resolved = self._resolve_vision_llm(conf)
            return dict(resolved) if isinstance(resolved, dict) else conf
        except Exception as e:
            logger.warning(f"[vision_media] 解析视觉 LLM 配置失败: {e}")
            return conf

    @staticmethod
    def _bounded_number(value, default, minimum, maximum, cast=float):
        try:
            parsed = cast(value)
            return max(minimum, min(maximum, parsed))
        except (TypeError, ValueError):
            return default

    def _vision_request_timeout(self, v_conf: dict) -> float:
        return self._bounded_number(
            v_conf.get("timeout_sec", 45),
            45.0,
            5.0,
            180.0,
        )

    def _vision_queue_timeout(self, v_conf: dict) -> float:
        return self._bounded_number(
            v_conf.get("queue_timeout_sec", 2),
            2.0,
            0.1,
            30.0,
        )

    def _vision_circuit_failure_threshold(self, v_conf: dict) -> int:
        return self._bounded_number(
            v_conf.get("circuit_breaker_failures", 2),
            2,
            1,
            20,
            int,
        )

    def _vision_circuit_cooldown(self, v_conf: dict) -> float:
        return self._bounded_number(
            v_conf.get("circuit_breaker_cooldown_sec", 120),
            120.0,
            5.0,
            1800.0,
        )

    def _vision_failure_fallback_text(self, v_conf: dict) -> str:
        text = str(
            v_conf.get("failure_fallback_text")
            or "识图服务当前响应超时或繁忙，本次未能可靠识别图片；请勿猜测具体角色或内容。"
        ).strip()
        return text[:240]

    async def _acquire_vision_slot(self, v_conf: dict) -> bool:
        now = time.monotonic()
        if self._vision_circuit_open_until > now:
            if now - self._vision_last_circuit_log_at >= 10:
                remaining = self._vision_circuit_open_until - now
                logger.warning(
                    "[vision_media] 视觉 API 熔断中，跳过请求，剩余 %.1f 秒",
                    remaining,
                )
                self._vision_last_circuit_log_at = now
            return False
        try:
            await asyncio.wait_for(
                self._vision_semaphore.acquire(),
                timeout=self._vision_queue_timeout(v_conf),
            )
        except TimeoutError:
            logger.warning(
                "[vision_media] 视觉 API 并发已满，排队超时后跳过请求"
            )
            return False

        now = time.monotonic()
        if self._vision_circuit_open_until > now:
            self._vision_semaphore.release()
            return False
        return True

    def _record_vision_success(self):
        self._vision_consecutive_failures = 0
        self._vision_circuit_open_until = 0.0

    def _record_vision_failure(self, v_conf: dict, reason: str):
        self._vision_consecutive_failures += 1
        threshold = self._vision_circuit_failure_threshold(v_conf)
        logger.warning(
            "[vision_media] 视觉 API 失败 (%s)，连续失败 %s/%s",
            reason,
            self._vision_consecutive_failures,
            threshold,
        )
        if self._vision_consecutive_failures < threshold:
            return
        cooldown = self._vision_circuit_cooldown(v_conf)
        self._vision_circuit_open_until = time.monotonic() + cooldown
        logger.warning(
            "[vision_media] 视觉 API 已熔断 %.1f 秒，期间请求将快速回退",
            cooldown,
        )

    def _is_safe_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            if not host:
                return False
            if host.lower() in ("localhost",):
                return False
            try:
                ip = ipaddress.ip_address(host)
                return not (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                )
            except ValueError:
                pass

            try:
                infos = socket.getaddrinfo(host, parsed.port or 80)
                for info in infos:
                    addr = info[4][0]
                    try:
                        ip = ipaddress.ip_address(addr)
                        if (
                            ip.is_private
                            or ip.is_loopback
                            or ip.is_link_local
                            or ip.is_reserved
                            or ip.is_multicast
                        ):
                            return False
                    except Exception:
                        continue
            except Exception:
                return False
            return True
        except Exception:
            return False

    def _vision_response_mode(self) -> str:
        try:
            m = str(self._get_conf("vision_llm_response_mode", "text") or "text").strip().lower()
            return m if m in ("text", "json") else "text"
        except Exception:
            return "text"

    def _vision_route_enabled(self) -> bool:
        try:
            return bool(self._get_conf("vision_route_enable", True))
        except Exception:
            return True

    def _vision_route_anime_score_threshold(self) -> int:
        try:
            v = int(self._get_conf("vision_route_anime_score_threshold", 70))
            return max(0, min(100, v))
        except Exception:
            return 70

    def _vision_route_simple_confidence_threshold(self) -> int:
        try:
            v = int(self._get_conf("vision_route_simple_confidence_threshold", 70))
            return max(0, min(100, v))
        except Exception:
            return 70

    def _vision_route_image_max_dim(self) -> int:
        try:
            v = int(self._get_conf("vision_route_image_max_dim", 384))
            return max(128, min(1280, v))
        except Exception:
            return 384

    def _vision_route_image_max_kb(self) -> int:
        try:
            v = int(self._get_conf("vision_route_image_max_kb", 80))
            return max(32, min(500, v))
        except Exception:
            return 80

    def _vision_route_max_output_tokens(self) -> int:
        try:
            v = int(self._get_conf("vision_route_max_output_tokens", 800))
            return max(128, min(2000, v))
        except Exception:
            return 800

    def _vision_fast_llm_config(self, v_conf: dict) -> dict | None:
        try:
            raw = self.config.get("vision_fast_llm", {})
            conf = dict(v_conf or {})
            route_max_tokens = self._vision_route_max_output_tokens()
            if isinstance(raw, dict):
                if raw.get("enable") is False:
                    return None
                for k in ("provider", "base_url", "api_key", "model", "format"):
                    v = raw.get(k)
                    if str(v or "").strip():
                        conf[k] = v
                if raw.get("max_output_tokens"):
                    route_max_tokens = int(raw.get("max_output_tokens"))
            if conf.get("enable") is False:
                return None
            has_endpoint = bool(str(conf.get("base_url") or "").strip())
            has_key = bool(str(conf.get("api_key") or "").strip())
            if not (has_endpoint or has_key):
                return None
            conf["enable_web_search"] = False
            conf["max_output_tokens"] = int(route_max_tokens)
            conf["prompt"] = self._build_vision_route_prompt()
            return conf
        except Exception:
            return None

    def _vision_route_should_try_fast(
        self,
        *,
        plain_text_content: str,
        img_src: str | None,
        file_token: str | None,
        v_conf: dict,
    ) -> bool:
        if not self._vision_route_enabled():
            return False
        if not self._vision_fast_llm_config(v_conf):
            return False
        if self._vision_route_is_obvious_anime_context(
            plain_text_content=plain_text_content,
            img_src=img_src,
            file_token=file_token,
        ):
            return False
        return True

    @staticmethod
    def _vision_route_is_obvious_anime_context(
        *,
        plain_text_content: str,
        img_src: str | None,
        file_token: str | None,
    ) -> bool:
        text = str(plain_text_content or "").strip().lower()
        path = f"{img_src or ''} {file_token or ''}".lower()
        if "astrbot_plugin_animewifex" in path or "/wife/" in path:
            return True
        zh_hits = (
            "二次元",
            "动漫",
            "番剧",
            "漫画",
            "角色是谁",
            "这是谁",
            "这个角色",
            "什么角色",
            "哪部番",
            "出处",
            "原作",
            "画师",
            "图源",
            "抽老婆",
            "老婆图",
            "手办",
        )
        if any(k in text for k in zh_hits):
            return True
        ascii_hits = ("vtuber", "vup", "galgame", "pixiv", "coser")
        if any(k in text for k in ascii_hits):
            return True
        if re.search(r"(?<![a-z])cos(?![a-z])", text):
            return True
        return False

    @staticmethod
    def _vision_route_score(value, default: int = 0) -> int:
        try:
            raw = str(value).strip()
            if not raw:
                return default
            f = float(raw.rstrip("%"))
            if 0.0 < f <= 1.0 and "." in raw:
                f *= 100.0
            return max(0, min(100, int(round(f))))
        except Exception:
            return default

    @staticmethod
    def _vision_route_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        s = str(value or "").strip().lower()
        return s in ("1", "true", "yes", "y", "是", "需要", "heavy")

    def _vision_route_decision(self, obj: dict | None) -> str:
        if not isinstance(obj, dict):
            return "heavy"
        route = str(obj.get("route") or "").strip().lower()
        desc = str(obj.get("desc") or "").strip()
        anime_score = self._vision_route_score(obj.get("anime_score"), 0)
        confidence = self._vision_route_score(obj.get("confidence"), 0)
        nsfw_score = self._vision_route_score(obj.get("nsfw_score"), 0)
        if not desc:
            return "heavy"
        if nsfw_score >= self._vision_nsfw_score_threshold():
            return "heavy"
        if self._vision_route_bool(obj.get("need_heavy")):
            return "heavy"
        if route in ("anime", "uncertain"):
            return "heavy"
        if anime_score >= self._vision_route_anime_score_threshold():
            return "heavy"
        if (
            route == "simple"
            and confidence >= self._vision_route_simple_confidence_threshold()
        ):
            return "simple"
        return "heavy"

    def _build_vision_route_prompt(self) -> str:
        return (
            "只输出一行 JSON，不要 Markdown、代码块、解释或前后缀。"
            "判断图片是否需要昂贵的二次元角色识别/作品识别链路。"
            "route 只能是 simple、anime、uncertain。"
            "anime=动漫/漫画/VTuber/galgame/手游角色/二次元插画/cos/需要识别角色或作品来源；"
            "simple=账单、网页截图、聊天截图、现实照片、食物、动物、普通表情包、游戏UI/战绩/装备截图、纯文字图，不需要角色考据；"
            "uncertain=无法确定或疑似二次元但不清楚。"
            "输出字段：route, anime_score(0-100), confidence(0-100), desc(50字内), "
            "essence_score(0-100), nsfw_score(0-100), need_heavy(boolean)。"
            "普通图 need_heavy=false；anime 或 uncertain 且需要识别角色/作品时 need_heavy=true。"
        )

    def _prepare_vision_route_images(self, images: list[str]) -> list[str]:
        if not images:
            return []
        first = str(images[0] or "").strip()
        if not first.startswith("data:image/") or "," not in first:
            return [first]
        try:
            head, payload = first.split(",", 1)
            if ";base64" not in head.lower():
                return [first]
            raw = base64.b64decode(payload)
            im = PILImage.open(io.BytesIO(raw))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            max_dim = self._vision_route_image_max_dim()
            w, h = im.size
            if max(w, h) > max_dim:
                ratio = max_dim / float(max(w, h))
                new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
                try:
                    im = im.resize(new_size, PILImage.Resampling.LANCZOS)
                except Exception:
                    im = im.resize(new_size)
            max_bytes = self._vision_route_image_max_kb() * 1024
            quality = 75
            last_bytes = b""
            for _ in range(8):
                buf = io.BytesIO()
                try:
                    im.save(buf, format="JPEG", quality=quality, optimize=True)
                except Exception:
                    im.save(buf, format="JPEG", quality=quality)
                data = buf.getvalue()
                if len(data) <= max_bytes:
                    b64 = base64.b64encode(data).decode("utf-8")
                    return [f"data:image/jpeg;base64,{b64}"]
                last_bytes = data
                quality = max(45, quality - 8)
                if quality == 45:
                    break
            if last_bytes:
                b64 = base64.b64encode(last_bytes).decode("utf-8")
                return [f"data:image/jpeg;base64,{b64}"]
        except Exception:
            pass
        return [first]

    def _vision_route_caption_json(self, obj: dict, desc: str) -> str | None:
        try:
            return json.dumps(
                {
                    "desc": desc,
                    "score": 0,
                    "route": str(obj.get("route") or "simple").strip() or "simple",
                    "anime_score": self._vision_route_score(obj.get("anime_score"), 0),
                    "confidence": self._vision_route_score(obj.get("confidence"), 0),
                    "nsfw_score": self._vision_route_score(obj.get("nsfw_score"), 0),
                    "need_heavy": self._vision_route_bool(obj.get("need_heavy")),
                },
                ensure_ascii=False,
            )
        except Exception:
            return None

    def _vision_essence_enabled(self) -> bool:
        try:
            return bool(self._get_conf("vision_essence_enable", False))
        except Exception:
            return False

    def _vision_essence_whitelist(self) -> set[str]:
        try:
            v = self._get_conf("vision_essence_group_whitelist", [])
            if isinstance(v, list):
                return set([str(x).strip() for x in v if str(x).strip()])
        except Exception:
            pass
        return set()

    def _vision_essence_cleanup_whitelist(self) -> set[str]:
        try:
            v = self._get_conf("vision_essence_cleanup_group_whitelist", [])
            if isinstance(v, list):
                return set([str(x).strip() for x in v if str(x).strip()])
        except Exception:
            pass
        return set()

    def _vision_essence_cleanup_retention_days(self) -> int:
        try:
            v = int(self._get_conf("vision_essence_cleanup_retention_days", 7))
            return max(0, v)
        except Exception:
            return 7

    def _vision_essence_cleanup_auto_enabled(self) -> bool:
        try:
            return bool(self._get_conf("vision_essence_cleanup_auto_enable", True))
        except Exception:
            return True

    def _vision_essence_cleanup_interval_seconds(self) -> int:
        try:
            v = int(self._get_conf("vision_essence_cleanup_interval_minutes", 30))
            return max(300, v * 60)
        except Exception:
            return 1800

    def _vision_marker_name(self) -> str:
        try:
            name = str(self._get_conf("vision_essence_marker", "vision_essence") or "").strip()
            return name or "vision_essence"
        except Exception:
            return "vision_essence"

    def _vision_essence_prompt_extra(self) -> str:
        try:
            return str(
                self._get_conf(
                    "vision_essence_prompt_extra",
                    "当图片是好看的色图（cos自拍、二次元高质量图）时 essence=1，否则 essence=0。",
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _vision_nsfw_enabled(self) -> bool:
        try:
            return bool(self._get_conf("vision_nsfw_enable", True))
        except Exception:
            return True

    def _vision_nsfw_withdraw_enabled(self) -> bool:
        try:
            return bool(self._get_conf("vision_nsfw_withdraw_enable", True))
        except Exception:
            return True

    def _vision_nsfw_score_threshold(self) -> int:
        try:
            v = int(self._get_conf("vision_nsfw_score_threshold", 85))
            return max(0, min(100, v))
        except Exception:
            return 85

    def _vision_simhash_enabled(self) -> bool:
        try:
            return bool(self._get_conf("vision_image_simhash_enable", True))
        except Exception:
            return True

    def _vision_simhash_max_hamming(self) -> int:
        try:
            v = int(self._get_conf("vision_image_simhash_max_hamming", 6))
            return max(0, min(20, v))
        except Exception:
            return 6

    def _vision_simhash_scan_limit(self) -> int:
        try:
            v = int(self._get_conf("vision_image_simhash_scan_limit", 3000))
            return max(100, min(20000, v))
        except Exception:
            return 3000

    def _vision_simhash_ui_strict_enable(self) -> bool:
        try:
            return bool(self._get_conf("vision_image_simhash_ui_strict_enable", True))
        except Exception:
            return True

    def _vision_simhash_ui_max_hamming(self) -> int:
        try:
            v = int(self._get_conf("vision_image_simhash_ui_max_hamming", 1))
            return max(0, min(10, v))
        except Exception:
            return 1

    @staticmethod
    def _looks_like_ui_screenshot_caption(caption: str) -> bool:
        s = str(caption or "").strip().lower()
        if not s:
            return False
        patterns = (
            "游戏",
            "截图",
            "界面",
            "客户端",
            "大厅",
            "ui",
            "hud",
            "战绩",
            "排位",
            "组队",
            "开黑",
            "lol",
            "英雄联盟",
            "dnf",
            "星穹铁道",
            "崩坏",
        )
        return any(p in s for p in patterns)

    def _dhash64_from_data_url(self, data_url: str, size: int = 8) -> str | None:
        try:
            s = str(data_url or "").strip()
            if not s.startswith("data:image/") or "," not in s:
                return None
            head, payload = s.split(",", 1)
            if ";base64" not in head.lower() or not payload:
                return None
            raw = base64.b64decode(payload)
            im = PILImage.open(io.BytesIO(raw)).convert("L")
            try:
                im = im.resize((size + 1, size), PILImage.Resampling.LANCZOS)
            except Exception:
                im = im.resize((size + 1, size))
            arr = list(im.getdata())
            bits = []
            w = size + 1
            h = size
            for y in range(h):
                row = arr[y * w : (y + 1) * w]
                for x in range(size):
                    bits.append("1" if row[x + 1] > row[x] else "0")
            bstr = "".join(bits)
            if len(bstr) != 64:
                return None
            val = int(bstr, 2)
            return f"dhash64:{val:016x}"
        except Exception:
            return None

    def _vision_nsfw_mark_text(self) -> str:
        try:
            v = str(self._get_conf("vision_nsfw_mark_text", "黄暴图片") or "").strip()
            return v or "黄暴图片"
        except Exception:
            return "黄暴图片"

    def _vision_nsfw_prompt_extra(self) -> str:
        try:
            return str(
                self._get_conf(
                    "vision_nsfw_prompt_extra",
                    "若出现严重漏点/露点、性器官、性交、色情暴力、未成年色情或其他违法性黄暴内容，视为黄暴图片。",
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _vision_essence_score_threshold(self) -> int:
        try:
            v = int(self._get_conf("vision_essence_score_threshold", 85))
            return max(0, min(100, v))
        except Exception:
            return 85

    def _extract_json_obj(self, text: str) -> dict | None:
        s = str(text or "").strip()
        if not s:
            return None

        def _try_parse(raw: str) -> dict | None:
            try:
                obj = json.loads(raw)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None

        def _normalize(obj: dict) -> dict | None:
            if not isinstance(obj, dict):
                return None
            resp = obj.get("response")
            if isinstance(resp, dict):
                return resp
            if isinstance(resp, str):
                parsed = _try_parse(resp.strip())
                if parsed:
                    return parsed
            return obj

        def _strip_comments(raw: str) -> str:
            raw = re.sub(r"<!--[\s\S]*?-->", "", raw)
            raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
            return raw

        def _strip_fences(raw: str) -> str:
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            return raw.strip()

        def _extract_blocks(raw: str) -> list[str]:
            blocks: list[str] = []
            depth = 0
            start = None
            in_str = False
            escape = False
            for i, ch in enumerate(raw):
                if in_str:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                    continue
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}" and depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        blocks.append(raw[start : i + 1])
            return blocks

        cleaned = _strip_comments(_strip_fences(s))
        obj = _try_parse(cleaned)
        if obj:
            return _normalize(obj)

        code_patterns = [
            r"```json\s*([\s\S]*?)\s*```",
            r"```\s*([\s\S]*?)\s*```",
        ]
        for pattern in code_patterns:
            matches = re.findall(pattern, s)
            if matches:
                for match in reversed(matches):
                    obj = _try_parse(_strip_comments(match.strip()))
                    if obj:
                        return _normalize(obj)

        blocks = _extract_blocks(cleaned)
        if blocks:
            for block in reversed(blocks):
                obj = _try_parse(block)
                if obj:
                    return _normalize(obj)

        try:
            match = re.search(r"(\{[\s\S]*\})", cleaned)
            if match:
                obj = _try_parse(match.group(1))
                if obj:
                    return _normalize(obj)
        except Exception:
            pass

        try:
            from astrbot_plugin_mem_chat.src.llm.response_schemas import StructuredResponse

            obj = StructuredResponse.extract_json_from_text(s)
            if obj:
                return _normalize(obj)
        except Exception:
            pass

        return None

    async def _try_set_essence_msg(
        self, event: AstrMessageEvent, group_id: str | None, message_id: str | int
    ) -> bool:
        try:
            mid = int(message_id)
        except Exception:
            return False
        gid = str(group_id or "").strip()
        bot = getattr(event, "bot", None)
        if not bot:
            return False
        action_result = await self._call_bot_action(
            bot, "set_essence_msg", treat_none_as_called=True, message_id=mid
        )

        if self._is_essence_full_result(action_result):
            cleaned = await self._cleanup_old_essence_msgs_for_group(
                event=event,
                group_id=gid,
            )
            if cleaned > 0:
                logger.warning(
                    "[vision_media] essence capacity reached, cleaned %s expired bot-owned essence msgs for group_id=%s before retry",
                    cleaned,
                    gid,
                )
                action_result = await self._call_bot_action(
                    bot, "set_essence_msg", treat_none_as_called=True, message_id=mid
                )
            else:
                logger.warning(
                    "[vision_media] essence capacity reached but no expired bot-owned essence msg found for group_id=%s",
                    gid,
                )

        if self._action_failed(action_result):
            return False

        # aiocqhttp 将 OneBot 的 status=async 也视为“成功返回”，
        # 这里额外查询精华列表，避免把“已受理但未真正生效”误记为成功。
        if not gid:
            return action_result is not None

        fetched_supported = False
        for _ in range(4):
            data = await self._fetch_essence_list(bot, gid)
            if data is not None:
                fetched_supported = True
            if isinstance(data, list) and self._essence_list_contains_event(
                data, event, message_id=mid
            ):
                return True
            await asyncio.sleep(0.6)
        if not fetched_supported:
            logger.warning(
                "[vision_media] set_essence_msg verification unavailable group_id=%s msg_id=%s: get_essence_msg_list unsupported or unreachable",
                gid,
                mid,
            )
        return False

    async def _call_bot_action(
        self, bot, action_name: str, *, treat_none_as_called: bool = False, **kwargs
    ):
        action_result = None
        try:
            fn = getattr(bot, action_name, None)
            if fn:
                action_result = await fn(**kwargs)
                if action_result is None and treat_none_as_called:
                    return {"status": "ok", "retcode": 0}
        except Exception:
            pass
        if action_result is not None:
            return action_result
        try:
            fn = getattr(bot, "call_action", None)
            if fn:
                action_result = await fn(action_name, **kwargs)
                if action_result is None and treat_none_as_called:
                    return {"status": "ok", "retcode": 0}
                return action_result
        except Exception:
            pass
        return None

    async def _fetch_essence_list(self, bot, group_id: str | None):
        gid = str(group_id or "").strip()
        if not gid or not gid.isdigit():
            return None
        return await self._call_bot_action(bot, "get_essence_msg_list", group_id=int(gid))

    def _action_failed(self, action_result) -> bool:
        if action_result is None:
            return True
        if isinstance(action_result, dict):
            status = str(action_result.get("status", "")).strip().lower()
            if status == "failed":
                return True
            retcode = action_result.get("retcode")
            if status and status not in ("ok", "async") and retcode not in (None, 0):
                return True
        return False

    def _is_essence_full_result(self, action_result) -> bool:
        if not isinstance(action_result, dict):
            return False
        if not self._action_failed(action_result):
            return False
        text = json.dumps(action_result, ensure_ascii=False, default=str).lower()
        keywords = ("精华", "digest", "上限", "已满", "满了", "full", "limit", "capacity")
        return any(k in text for k in keywords)

    def _safe_int(self, value) -> int | None:
        try:
            if value is None:
                return None
            return int(str(value).strip())
        except Exception:
            return None

    def _extract_event_real_seq(self, event: AstrMessageEvent) -> int | None:
        try:
            msg_obj = getattr(event, "message_obj", None)
            raw = getattr(msg_obj, "raw_message", None)
            if raw is None:
                return None
            if hasattr(raw, "get"):
                return self._safe_int(raw.get("real_seq"))
        except Exception:
            pass
        return None

    def _extract_event_plain_text(self, event: AstrMessageEvent) -> str:
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            texts: list[str] = []
            for seg in chain:
                if isinstance(seg, Plain):
                    txt = str(getattr(seg, "text", "") or "")
                    if txt:
                        texts.append(txt)
            return "".join(texts).strip()
        except Exception:
            return str(getattr(event, "message_str", "") or "").strip()

    def _extract_event_image_names(self, event: AstrMessageEvent) -> set[str]:
        names: set[str] = set()
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            for seg in chain:
                if isinstance(seg, Image):
                    file_name = str(getattr(seg, "file", "") or "").strip().lower()
                    if file_name:
                        names.add(file_name)
                    url = str(getattr(seg, "url", "") or "").strip().lower()
                    if url:
                        names.add(os.path.basename(urlparse(url).path))
        except Exception:
            pass
        return names

    def _essence_list_contains_event(
        self,
        data: list,
        event: AstrMessageEvent,
        *,
        message_id: int | None = None,
    ) -> bool:
        event_seq = self._extract_event_real_seq(event)
        sender_id = self._safe_int(event.get_sender_id())
        plain_text = self._extract_event_plain_text(event)
        image_names = self._extract_event_image_names(event)
        for item in data:
            if not isinstance(item, dict):
                continue
            item_seq = self._safe_int(item.get("msg_seq"))
            if event_seq is not None and item_seq == event_seq:
                return True
            if sender_id is not None and self._safe_int(item.get("sender_id")) != sender_id:
                continue
            content = item.get("content")
            if plain_text and isinstance(content, list):
                content_text = "".join(
                    str(
                        ((part.get("data") or {}).get("text", ""))
                    )
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
                if content_text == plain_text:
                    return True
            if image_names and isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "image":
                        continue
                    part_data = part.get("data") or {}
                    file_name = str(part_data.get("file", "") or "").strip().lower()
                    url = str(part_data.get("url", "") or "").strip().lower()
                    digest_name = os.path.basename(urlparse(url).path) if url else ""
                    if file_name in image_names or digest_name in image_names:
                        return True
            item_mid = self._safe_int(item.get("message_id"))
            if message_id is not None and item_mid == message_id:
                return True
        return False

    async def _delete_essence_msg(self, bot, message_id: int) -> bool:
        result = await self._call_bot_action(bot, "delete_essence_msg", message_id=message_id)
        return not self._action_failed(result)

    async def _cleanup_old_essence_msgs_for_group(
        self, *, event: AstrMessageEvent, group_id: str
    ) -> int:
        self_id = self._safe_int(event.get_self_id())
        return await self.cleanup_expired_essence_msgs_for_group(
            bot=getattr(event, "bot", None),
            self_id=self_id,
            group_id=group_id,
        )

    async def cleanup_expired_essence_msgs_for_group(
        self, *, bot, self_id: int | None, group_id: str
    ) -> int:
        gid = str(group_id or "").strip()
        if not gid:
            return 0
        if gid not in self._vision_essence_cleanup_whitelist():
            return 0
        if not bot:
            return 0
        data = await self._fetch_essence_list(bot, gid)
        if not isinstance(data, list) or not data:
            return 0
        if self_id is None:
            return 0
        retention_days = self._vision_essence_cleanup_retention_days()
        cutoff_ts = max(0, int(time.time()) - retention_days * 86400)
        candidates: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if not self._is_bot_owned_essence_item(item, self_id):
                continue
            operator_time = self._safe_int(item.get("operator_time")) or 0
            if operator_time > cutoff_ts:
                continue
            candidates.append(item)
        if not candidates:
            return 0
        candidates.sort(
            key=lambda item: (
                self._safe_int(item.get("operator_time")) or 0,
                self._safe_int(item.get("msg_seq")) or 0,
                self._safe_int(item.get("message_id")) or 0,
            )
        )
        removed = 0
        for item in candidates:
            essence_mid = self._safe_int(item.get("message_id"))
            if essence_mid is None:
                continue
            ok = await self._delete_essence_msg(bot, essence_mid)
            if ok:
                removed += 1
                await asyncio.sleep(0.3)
        return removed

    def _is_bot_owned_essence_item(self, item: dict, self_id: int | None) -> bool:
        if self_id is None or not isinstance(item, dict):
            return False
        operator_id = self._safe_int(item.get("operator_id"))
        return operator_id is not None and operator_id == self_id

    async def cleanup_expired_essence_msgs_for_groups(
        self, *, bot, self_id: int | None, group_ids: list[str] | set[str] | tuple[str, ...]
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        if not bot or self_id is None:
            return result
        seen: set[str] = set()
        for group_id in group_ids:
            gid = str(group_id or "").strip()
            if not gid or gid in seen:
                continue
            seen.add(gid)
            removed = await self.cleanup_expired_essence_msgs_for_group(
                bot=bot,
                self_id=self_id,
                group_id=gid,
            )
            if removed > 0:
                result[gid] = removed
        return result

    async def _try_withdraw_msg(
        self, event: AstrMessageEvent, message_id: str | int
    ) -> bool:
        try:
            mid = int(message_id)
        except Exception:
            return False
        bot = getattr(event, "bot", None)
        if not bot:
            return False
        try:
            fn = getattr(bot, "delete_msg", None)
            if fn:
                await fn(message_id=mid)
                return True
        except Exception:
            pass
        try:
            fn = getattr(bot, "call_action", None)
            if fn:
                await fn("delete_msg", message_id=mid)
                return True
        except Exception:
            pass
        try:
            fn = getattr(bot, "call_action", None)
            if fn:
                await fn("recall_msg", message_id=mid)
                return True
        except Exception:
            return False
        return False

    async def _maybe_set_essence_for_image(
        self,
        *,
        event: AstrMessageEvent,
        group_id: str,
        image_key: str | None,
        msg_id: str | None,
        score: int,
    ):
        try:
            s = int(score or 0)
        except Exception:
            s = 0
        if s < self._vision_essence_score_threshold():
            logger.info(
                "[vision_media] skip set_essence_msg: score=%s < threshold=%s group_id=%s msg_id=%s",
                s,
                self._vision_essence_score_threshold(),
                str(group_id or "").strip() or "-",
                str(msg_id or "").strip() or "-",
            )
            return
        gid = str(group_id or "").strip()
        if not gid:
            logger.info(
                "[vision_media] skip set_essence_msg: missing group_id msg_id=%s score=%s",
                str(msg_id or "").strip() or "-",
                s,
            )
            return
        if not self._vision_essence_enabled():
            logger.info(
                "[vision_media] skip set_essence_msg: vision_essence_enable=false group_id=%s msg_id=%s score=%s",
                gid,
                str(msg_id or "").strip() or "-",
                s,
            )
            return
        whitelist = self._vision_essence_whitelist()
        if gid not in whitelist:
            logger.info(
                "[vision_media] skip set_essence_msg: group_id=%s not in whitelist=%s msg_id=%s score=%s",
                gid,
                sorted(whitelist),
                str(msg_id or "").strip() or "-",
                s,
            )
            return
        if msg_id is None:
            logger.warning(
                "[vision_media] skip set_essence_msg: missing msg_id group_id=%s score=%s",
                gid,
                s,
            )
            return
        ik = str(image_key or "").strip() or None
        if self._image_cache and ik:
            try:
                if self._image_cache.essence_already_set(group_id=gid, image_key=ik):
                    logger.info(
                        "[vision_media] skip set_essence_msg: already_set group_id=%s image_key=%s msg_id=%s score=%s",
                        gid,
                        ik,
                        str(msg_id or "").strip() or "-",
                        s,
                    )
                    return
            except Exception:
                pass
        ok = await self._try_set_essence_msg(event, gid, msg_id)
        if ok:
            logger.info(
                "[vision_media] set_essence_msg success group_id=%s image_key=%s msg_id=%s score=%s",
                gid,
                ik or "-",
                str(msg_id or "").strip() or "-",
                s,
            )
        else:
            logger.warning(
                "[vision_media] set_essence_msg failed group_id=%s image_key=%s msg_id=%s score=%s",
                gid,
                ik or "-",
                str(msg_id or "").strip() or "-",
                s,
            )
        if ok and self._image_cache and ik:
            try:
                self._image_cache.mark_essence_set(group_id=gid, image_key=ik, msg_id=str(msg_id))
            except Exception:
                pass

    async def _compress_image(
        self, data: bytes, max_kb: int | None = None
    ) -> tuple[bytes, str]:
        if not max_kb:
            try:
                max_kb = int(self._get_conf("mem_chat_llm_image_max_kb", 2048))
            except Exception:
                max_kb = 2048
        max_bytes = int(max_kb) * 1024
        try:
            img = PILImage.open(io.BytesIO(data))
            mime_type = "image/jpeg"
            if img.format:
                fmt = img.format.lower()
                if fmt == "png":
                    mime_type = "image/png"
                elif fmt == "gif":
                    mime_type = "image/gif"
                elif fmt == "webp":
                    mime_type = "image/webp"

            if len(data) <= max_bytes:
                return data, mime_type

            if mime_type == "image/gif":
                return data, mime_type

            max_dim = int(self._get_conf("mem_chat_llm_image_max_dim", 4096))
            init_quality = int(self._get_conf("mem_chat_llm_image_quality_init", 90))
            min_quality = int(self._get_conf("mem_chat_llm_image_quality_min", 65))

            w, h = img.size
            if max(w, h) > max_dim:
                ratio = max_dim / float(max(w, h))
                new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
                try:
                    img = img.resize(new_size, PILImage.Resampling.LANCZOS)
                except Exception:
                    img = img.resize(new_size)

            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            q = init_quality
            step = 7
            last_bytes = data
            for _ in range(10):
                buf = io.BytesIO()
                try:
                    img.save(buf, format="JPEG", quality=q, optimize=True)
                except Exception:
                    img.save(buf, format="JPEG", quality=q)
                new_data = buf.getvalue()
                if len(new_data) <= max_bytes:
                    return new_data, "image/jpeg"
                last_bytes = new_data
                q = max(min_quality, q - step)
                if q == min_quality:
                    break

            return last_bytes, "image/jpeg"
        except Exception as e:
            logger.error(f"[vision_media] Failed to compress image: {e}")
            return data, "image/jpeg"

    async def image_to_base64_frames(self, file_path_or_url: str) -> list[str]:
        file_path_or_url = file_path_or_url.strip()
        if file_path_or_url.startswith("`") and file_path_or_url.endswith("`"):
            file_path_or_url = file_path_or_url.strip("`").strip()

        try:
            data: bytes | None = None
            if file_path_or_url.startswith("data:image/"):
                try:
                    head, b64_data = file_path_or_url.split(",", 1)
                    if ";base64" in head.lower() and b64_data:
                        data = base64.b64decode(b64_data)
                except Exception as e:
                    logger.error(
                        f"[vision_media] Failed to decode data URL image for frames: {e}"
                    )
                    data = None
            elif file_path_or_url.startswith("http"):
                try:
                    async with aiohttp.ClientSession(trust_env=True) as session:
                        async with session.get(file_path_or_url, timeout=20) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                            else:
                                logger.warning(
                                    f"[vision_media] Failed to download image for frames: {resp.status}"
                                )
                except Exception as e:
                    logger.error(f"[vision_media] Error downloading image for frames: {e}")
                    data = None
            elif os.path.exists(file_path_or_url):
                try:
                    with open(file_path_or_url, "rb") as f:
                        data = f.read()
                except Exception as e:
                    logger.error(f"[vision_media] Failed to read image file for frames: {e}")
                    data = None
            if not data:
                return []

            max_kb = int(self._get_conf("mem_chat_llm_image_max_kb", 2048))
            max_dim = int(self._get_conf("mem_chat_llm_image_max_dim", 4096))
            init_quality = int(self._get_conf("mem_chat_llm_image_quality_init", 90))
            min_quality = int(self._get_conf("mem_chat_llm_image_quality_min", 65))
            gif_max_frames = int(self._get_conf("mem_chat_llm_gif_max_frames", 6))
            gif_total_max_kb = int(self._get_conf("mem_chat_llm_gif_total_max_kb", 3072))

            try:
                img = PILImage.open(io.BytesIO(data))
            except Exception:
                try:
                    data, mime = await self._compress_image(data, max_kb)
                    b64 = base64.b64encode(data).decode("utf-8")
                    return [f"data:{mime};base64,{b64}"]
                except Exception:
                    return []

            is_animated = bool(getattr(img, "is_animated", False)) and (
                str(getattr(img, "format", "") or "").upper() == "GIF"
            )

            def _compress_pil_to_jpeg(im: PILImage.Image, max_bytes: int) -> bytes:
                try:
                    w, h = im.size
                    if max(w, h) > max_dim:
                        ratio = max_dim / float(max(w, h))
                        new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
                        try:
                            im = im.resize(new_size, PILImage.Resampling.LANCZOS)
                        except Exception:
                            im = im.resize(new_size)
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    q = init_quality
                    step = 7
                    last_bytes = b""
                    for _ in range(10):
                        buf = io.BytesIO()
                        try:
                            im.save(buf, format="JPEG", quality=q, optimize=True)
                        except Exception:
                            im.save(buf, format="JPEG", quality=q)
                        b = buf.getvalue()
                        if len(b) <= max_bytes:
                            return b
                        last_bytes = b
                        q = max(min_quality, q - step)
                        if q == min_quality:
                            break
                    return last_bytes
                except Exception:
                    return b""

            def _extract_gif_frame_indices(frame_count: int, take: int) -> list[int]:
                if frame_count <= 0:
                    return [0]
                if take <= 1 or frame_count == 1:
                    return [0]
                take = min(take, frame_count)
                idxs: list[int] = []
                for i in range(take):
                    pos = int(round(i * (frame_count - 1) / float(take - 1)))
                    if not idxs or idxs[-1] != pos:
                        idxs.append(pos)
                return idxs

            if is_animated:
                try:
                    try:
                        frame_count = int(getattr(img, "n_frames", 1) or 1)
                    except Exception:
                        frame_count = 1
                    take = max(1, min(gif_max_frames, frame_count))
                    idxs = _extract_gif_frame_indices(frame_count, take)
                    total_budget = max(1, gif_total_max_kb) * 1024
                    per_budget = max(1, total_budget // max(1, len(idxs)))
                    per_budget = min(per_budget, max_kb * 1024)

                    out_urls: list[str] = []
                    for idx in idxs:
                        try:
                            img.seek(idx)
                            frame = img.convert("RGB")
                        except Exception:
                            continue
                        jb = _compress_pil_to_jpeg(frame, per_budget)
                        if not jb:
                            continue
                        try:
                            b64 = base64.b64encode(jb).decode("utf-8")
                            out_urls.append(f"data:image/jpeg;base64,{b64}")
                        except Exception:
                            continue
                    if out_urls:
                        logger.info(
                            f"[vision_media] Extracted {len(out_urls)} frames from GIF for vision LLM."
                        )
                        return out_urls
                except Exception as e:
                    logger.error(f"[vision_media] Failed to extract GIF frames: {e}")

            try:
                max_bytes = int(max_kb) * 1024
                try:
                    still = img.convert("RGB")
                except Exception:
                    still = img
                    if getattr(still, "mode", None) not in ("RGB", "L"):
                        still = still.convert("RGB")
                jb = _compress_pil_to_jpeg(still, max_bytes)
                if not jb:
                    return []
                b64 = base64.b64encode(jb).decode("utf-8")
                return [f"data:image/jpeg;base64,{b64}"]
            except Exception:
                return []
        except Exception as e:
            logger.error(f"[vision_media] Unexpected error in image_to_base64_frames: {e}")
            return []

    async def download_image(self, url: str) -> str | None:
        try:
            url = url.strip("`\"' .,;:)")

            if not self._is_safe_url(url):
                logger.warning(f"[vision_media] Blocked unsafe URL: {url}")
                return None

            logger.info(
                f"[vision_media] Downloading image: {url} | System environment proxy enabled"
            )

            parsed_uri = urlparse(url)
            domain = f"{parsed_uri.scheme}://{parsed_uri.netloc}/"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": domain,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            }

            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        data, mime_type = await self._compress_image(data)
                    else:
                        logger.warning(
                            f"[vision_media] Failed to download image {url}: {resp.status}"
                        )
                        return None

                    suffix = ".jpg"
                    if "png" in mime_type:
                        suffix = ".png"
                    elif "gif" in mime_type:
                        suffix = ".gif"
                    elif "webp" in mime_type:
                        suffix = ".webp"

                    fd, path = tempfile.mkstemp(suffix=suffix)

                    with os.fdopen(fd, "wb") as f:
                        f.write(data)

                    logger.info(f"[vision_media] Downloaded image to {path}")
                    return path
        except Exception as e:
            logger.error(f"[vision_media] Error downloading image {url}: {e}")
            return None

    async def image_to_base64(self, file_path_or_url: str) -> str | None:
        frames = await self.image_to_base64_frames(file_path_or_url)
        if frames:
            return frames[0]
        return None

    def _build_vision_llm_request(
        self,
        images: list[str],
        v_conf: dict,
        *,
        prompt_override: str | None = None,
        want_json: bool = False,
    ) -> tuple[str, dict, dict]:
        api_key = v_conf.get("api_key", "")
        base_url = v_conf.get("base_url", "https://api.openai.com/v1").rstrip("/")
        format_type = v_conf.get("format", "openai")
        model = v_conf.get("model", "gpt-4o-mini")
        prompt_text = (
            prompt_override
            if prompt_override is not None
            else v_conf.get("prompt", "请简要描述这张图片的内容，50字以内。")
        )

        is_multi = len(images) > 1
        if is_multi:
            tip = "这是一组gif，请按时间顺序理解动作、表情与场景变化。"
            pt = str(prompt_text or "").strip()
            if tip not in pt:
                prompt_text = (pt + "\n" + tip) if pt else tip

        if format_type == "google":
            if "/v1/chat/completions" in base_url:
                base_url = base_url.replace("/v1/chat/completions", "")

            parts = []
            if prompt_text:
                parts.append({"text": prompt_text})
            for img in images:
                mime_type = "image/jpeg"
                b64_data = img
                if "," in img:
                    try:
                        head, b64_data = img.split(",", 1)
                        if head.startswith("data:") and ";base64" in head:
                            mime_type = head.split("data:", 1)[1].split(";", 1)[0]
                    except Exception:
                        b64_data = img
                parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})

            url = f"{base_url}/v1beta/models/{model}:generateContent"
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["x-goog-api-key"] = api_key
            payload = {"contents": [{"role": "user", "parts": parts}]}
            if want_json:
                payload["generationConfig"] = {"responseMimeType": "application/json"}
            return url, headers, payload

        if format_type == "volces_responses":
            # 火山方舟 /api/v3/responses 接口，支持 web_search 工具
            if base_url.endswith("/api/v3/responses"):
                url = base_url
            elif base_url.endswith("/api/v3"):
                url = base_url + "/responses"
            else:
                url = base_url.rstrip("/") + "/api/v3/responses"

            input_contents = []
            if prompt_text:
                input_contents.append({"type": "input_text", "text": prompt_text})
            for img in images:
                # 火山方舟支持直接传 data URL 或 https URL
                input_contents.append({"type": "input_image", "image_url": img})

            payload = {
                "model": model,
                "input": [{"role": "user", "content": input_contents}],
            }
            # 是否开启 web 搜索
            if bool(v_conf.get("enable_web_search", True)):
                tools = [{"type": "web_search"}]
                # 可选搜索配置
                ws_opts = v_conf.get("web_search_options") or {}
                if isinstance(ws_opts, dict) and ws_opts:
                    tools[0].update(ws_opts)
                payload["tools"] = tools
            try:
                mt = int(v_conf.get("max_output_tokens") or 0)
                if mt > 0:
                    payload["max_output_tokens"] = mt
            except Exception:
                pass
            if want_json:
                # ark responses 接口暂不强制 json mode；靠 prompt 约束
                pass
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            return url, headers, payload

        if format_type == "anthropic":
            if base_url.endswith("/v1/messages"):
                url = base_url
            elif base_url.endswith("/v1"):
                url = base_url + "/messages"
            else:
                url = base_url + "/v1/messages"

            contents = []
            for img in images:
                mime_type = "image/jpeg"
                image_data = img
                if str(img).startswith(("http://", "https://")):
                    source = {"type": "url", "url": img}
                else:
                    if "," in str(img):
                        try:
                            head, image_data = str(img).split(",", 1)
                            if head.startswith("data:") and ";base64" in head:
                                mime_type = head.split("data:", 1)[1].split(";", 1)[0]
                        except Exception:
                            image_data = img
                    source = {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": image_data,
                    }
                contents.append({"type": "image", "source": source})
            if prompt_text:
                contents.append({"type": "text", "text": prompt_text})

            try:
                max_tokens = int(v_conf.get("max_output_tokens") or 512)
            except (TypeError, ValueError):
                max_tokens = 512
            payload = {
                "model": model,
                "max_tokens": max(1, max_tokens),
                "messages": [{"role": "user", "content": contents}],
            }
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            if api_key:
                headers["x-api-key"] = api_key
            return url, headers, payload

        url = base_url
        if not url.endswith("/v1/chat/completions"):
            url = url + "/v1/chat/completions"

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        contents = []
        if prompt_text:
            contents.append({"type": "text", "text": prompt_text})
        is_deepseek = "deepseek" in f"{base_url} {model}".lower()
        for img in images:
            image_url = {"url": img}
            if is_deepseek:
                image_url["detail"] = "original"
            contents.append({"type": "image_url", "image_url": image_url})
        payload = {"model": model, "messages": [{"role": "user", "content": contents}]}
        if is_deepseek:
            payload["thinking"] = {
                "type": (
                    "enabled"
                    if bool(v_conf.get("thinking_enabled", False))
                    else "disabled"
                )
            }
        try:
            max_tokens = int(v_conf.get("max_output_tokens") or 0)
            if max_tokens > 0:
                payload["max_tokens"] = max_tokens
        except (TypeError, ValueError):
            pass
        if want_json:
            payload["response_format"] = {"type": "json_object"}
        return url, headers, payload

    def _build_vision_json_prompt(self, base_prompt: str) -> str:
        p = str(base_prompt or "").strip()
        extra = self._vision_essence_prompt_extra()
        nsfw_extra = self._vision_nsfw_prompt_extra()
        payload = {
            "desc": "图片内容描述，尽量包含图片文字、角色/人物、发送者情绪意图，120字以内",
            "score": "0-100整数。对“值得群精华”的程度打分（高质量cos自拍/二次元高质量图才可能高分）",
            "nsfw_score": "0-100整数。黄暴/漏点/色情/暴力色情程度评分",
            "violation": "如出现黄暴违规则填“黄暴图片”，否则留空或“无”",
            "reason": "若违规请给出简短原因",
        }
        schema_hint = json.dumps(payload, ensure_ascii=False)
        rules = [
            "你必须只输出 JSON，不要输出 Markdown、代码块、解释、前后缀。",
            f"输出格式示例：{{\"desc\":\"...\",\"score\":0}}",
            f"字段含义：{schema_hint}",
            "严格标准：宁可低分也不要误判。以下情况一般给 0-30 分：游戏截图/有明显UI遮挡/裁切不完整/仅头像或半身/普通立绘/普通风景/无明显审美价值。",
            "中等 40-70：完整清晰但普通的二次元插画/截图，无强烈冲击力。",
            "高分 85-100：画面完整清晰、构图光影优秀、主体突出，属于高质量 cos 自拍或高质量二次元图，且更像“值得收藏/精华”的作品。",
            f"黄暴评分 nsfw_score >= {self._vision_nsfw_score_threshold()} 时，violation 必须为“黄暴图片”，并填写 reason。",
            "严重漏点或明显色情/暴力色情/非法内容即视为违规。",
        ]
        if extra:
            rules.append(extra)
        if nsfw_extra:
            rules.append(nsfw_extra)
        if p:
            return p + "\n" + "\n".join(rules)
        return "\n".join(rules)

    def _extract_nsfw_info(self, obj: dict) -> tuple[bool, str | None, int, str | None]:
        if not isinstance(obj, dict):
            return False, None, 0, None
        try:
            nsfw_score = int(obj.get("nsfw_score") or 0)
        except Exception:
            nsfw_score = 0
        nsfw_score = max(0, min(100, nsfw_score))
        violation = str(obj.get("violation") or "").strip()
        reason = str(obj.get("reason") or "").strip()
        if not self._vision_nsfw_enabled():
            return False, violation or None, nsfw_score, reason or None
        flags = ["黄暴", "违规", "敏感", "NSFW", "色情", "裸露", "露点", "性", "暴力色情", "未成年"]
        hit_text = violation or ""
        hit_reason = reason or ""
        hit = any(k in hit_text for k in flags) or any(k in hit_reason for k in flags)
        if nsfw_score >= self._vision_nsfw_score_threshold():
            hit = True
        return hit, violation or None, nsfw_score, reason or None

    def _extract_vision_text(self, data: dict, v_conf: dict) -> str:
        try:
            fmt = v_conf.get("format")
            if fmt == "google":
                return (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
            if fmt == "volces_responses":
                # 优先使用 output_text 字段（如果存在）
                ot = data.get("output_text")
                if isinstance(ot, str) and ot.strip():
                    return ot
                if isinstance(ot, list) and ot:
                    joined = "".join([str(x) for x in ot if isinstance(x, str)])
                    if joined.strip():
                        return joined
                # 通用解析：output[*].content[*].text，跳过 web_search_call 等非文本块
                pieces: list[str] = []
                for item in data.get("output", []) or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") and item.get("type") != "message":
                        continue
                    for c in item.get("content", []) or []:
                        if not isinstance(c, dict):
                            continue
                        ctype = c.get("type") or ""
                        if ctype in ("output_text", "text"):
                            t = c.get("text") or ""
                            if isinstance(t, str) and t:
                                pieces.append(t)
                if pieces:
                    return "".join(pieces)
                # 兜底：有些响应把文本直接放在 message.content
                msg = data.get("message") or {}
                if isinstance(msg, dict):
                    mc = msg.get("content")
                    if isinstance(mc, str):
                        return mc
                return ""
            if fmt == "anthropic":
                pieces = []
                for item in data.get("content", []) or []:
                    if not isinstance(item, dict) or item.get("type") != "text":
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        pieces.append(text)
                return "".join(pieces)
            if isinstance(data.get("text"), str):
                return data.get("text", "")
            if isinstance(data.get("content"), str):
                return data.get("content", "")
            return (
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
        except Exception:
            return ""

    @asynccontextmanager
    async def _post_vision_request(self, session, url, headers, payload, v_conf):
        """Try the configured vision endpoints before recording a request failure.

        Args:
            session: Existing HTTP session used by the vision request.
            url: Primary request URL.
            headers: Primary authentication headers.
            payload: Prepared model request, including images and output format.
            v_conf: Vision settings with an optional OpenAI-compatible backup.

        Yields:
            The first successful response, or the final HTTP failure response.
        """
        requests = [(url, headers, payload)]
        if v_conf.get("fallback_enabled") and url.endswith("/v1/chat/completions"):
            base = str(v_conf.get("fallback_base_url") or "").rstrip("/")
            key = str(v_conf.get("fallback_api_key") or "")
            if base and key:
                backup_url = base + (
                    "/chat/completions"
                    if base.endswith("/v1")
                    else "/v1/chat/completions"
                )
                requests.append(
                    (
                        backup_url,
                        {"Authorization": f"Bearer {key}"},
                        {
                            **payload,
                            "model": v_conf.get("fallback_model")
                            or payload.get("model"),
                        },
                    )
                )
        deadline = time.monotonic() + self._vision_request_timeout(v_conf)
        for index, (target, auth, body) in enumerate(requests):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                response = await session.post(
                    target, headers=auth, json=body, timeout=remaining
                )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if index == len(requests) - 1:
                    raise
                logger.warning(
                    "[vision_media] Vision endpoint unavailable: host=%s",
                    urlparse(target).hostname,
                )
                continue
            async with response:
                if response.status == 200 or index == len(requests) - 1:
                    yield response
                    return
                logger.warning(
                    "[vision_media] Vision endpoint failed: host=%s HTTP=%d",
                    urlparse(target).hostname,
                    response.status,
                )

    async def call_vision_llm(self, images: list[str], v_conf: dict) -> str:
        if not await self._acquire_vision_slot(v_conf):
            return ""
        req_timeout = self._vision_request_timeout(v_conf)
        try:
            url, headers, payload = self._build_vision_llm_request(images, v_conf)
            trust_env = not bool(v_conf.get("bypass_proxy", False))
            async with aiohttp.ClientSession(trust_env=trust_env) as session:
                async with self._post_vision_request(
                    session, url, headers, payload, v_conf
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = self._extract_vision_text(data, v_conf)
                        content = str(content or "").strip()
                        if not content:
                            logger.warning(
                                f"[vision_media] Vision LLM returned empty content. Data: {data}"
                            )
                            self._record_vision_failure(v_conf, "empty response")
                            return ""
                        self._record_vision_success()
                        logger.info(f"[vision_media] Vision LLM success: {content}")
                        return f"[图片内容: {content}]"
                    text = await resp.text()
                    self._record_vision_failure(v_conf, f"HTTP {resp.status}")
                    logger.error(
                        f"[vision_media] Vision LLM failed: {resp.status} - {text[:500]}"
                    )
        except asyncio.TimeoutError:
            self._record_vision_failure(
                v_conf,
                f"timeout after {req_timeout:.1f}s",
            )
            logger.warning(
                "[vision_media] 视觉 API 请求超过 %.1f 秒，已取消并回退",
                req_timeout,
            )
        except Exception as e:
            self._record_vision_failure(v_conf, type(e).__name__)
            logger.error(f"[vision_media] Vision LLM error: {e}")
        finally:
            self._vision_semaphore.release()

        return ""

    async def call_vision_llm_json(self, images: list[str], v_conf: dict) -> dict | None:
        if not await self._acquire_vision_slot(v_conf):
            return None
        req_timeout = self._vision_request_timeout(v_conf)
        try:
            prompt_text = self._build_vision_json_prompt(v_conf.get("prompt", ""))
            url, headers, payload = self._build_vision_llm_request(
                images, v_conf, prompt_override=prompt_text, want_json=True
            )
            trust_env = not bool(v_conf.get("bypass_proxy", False))
            async with aiohttp.ClientSession(trust_env=trust_env) as session:
                async with self._post_vision_request(
                    session, url, headers, payload, v_conf
                ) as resp:
                    if resp.status == 200:
                        data = None
                        content = ""
                        try:
                            data = await resp.json()
                        except Exception:
                            text = await resp.text()
                            if text:
                                try:
                                    data = json.loads(text)
                                except Exception:
                                    content = text.strip()
                        if not content and isinstance(data, dict):
                            content = str(self._extract_vision_text(data, v_conf) or "").strip()
                        if not content:
                            logger.warning(
                                f"[vision_media] Vision LLM returned empty json. Data: {data}"
                            )
                            self._record_vision_failure(v_conf, "empty response")
                            return None
                        obj = self._extract_json_obj(content)
                        if not obj:
                            logger.warning(
                                f"[vision_media] Vision LLM json parse failed. Content: {content}"
                            )
                            self._record_vision_failure(v_conf, "invalid JSON")
                            return None
                        try:
                            desc = str(obj.get("desc") or "").strip()
                            try:
                                score = int(obj.get("score") or 0)
                            except Exception:
                                score = 0
                            if not obj.get("score") and "essence" in obj:
                                try:
                                    score = 100 if int(obj.get("essence") or 0) == 1 else 0
                                except Exception:
                                    score = 0
                            score = max(0, min(100, score))
                            desc_one_line = re.sub(r"\s+", " ", desc).strip()
                            if len(desc_one_line) > 160:
                                desc_one_line = desc_one_line[:160] + "..."
                            logger.info(
                                f"[vision_media] Vision LLM json success: score={score} desc={desc_one_line}"
                            )
                        except Exception:
                            pass
                        self._record_vision_success()
                        return obj
                    text = await resp.text()
                    if isinstance(payload, dict) and "response_format" in payload:
                        payload2 = dict(payload)
                        payload2.pop("response_format", None)
                        async with self._post_vision_request(
                            session, url, headers, payload2, v_conf
                        ) as resp2:
                            if resp2.status == 200:
                                data2 = await resp2.json()
                                content2 = str(
                                    self._extract_vision_text(data2, v_conf) or ""
                                ).strip()
                                obj2 = self._extract_json_obj(content2)
                                if obj2:
                                    self._record_vision_success()
                                    return obj2
                            text2 = await resp2.text()
                            self._record_vision_failure(
                                v_conf,
                                f"HTTP {resp.status}/{resp2.status}",
                            )
                            logger.error(
                                f"[vision_media] Vision LLM json failed: {resp2.status} - {text2[:500]}"
                            )
                    else:
                        self._record_vision_failure(v_conf, f"HTTP {resp.status}")
                        logger.error(
                            f"[vision_media] Vision LLM json failed: {resp.status} - {text[:500]}"
                        )
        except asyncio.TimeoutError:
            self._record_vision_failure(
                v_conf,
                f"timeout after {req_timeout:.1f}s",
            )
            logger.warning(
                "[vision_media] 视觉 API JSON 请求超过 %.1f 秒，已取消并回退",
                req_timeout,
            )
        except Exception as e:
            self._record_vision_failure(v_conf, type(e).__name__)
            logger.error(f"[vision_media] Vision LLM json error: {e}")
        finally:
            self._vision_semaphore.release()
        return None

    async def call_vision_route_llm(self, images: list[str], v_conf: dict) -> dict | None:
        if not await self._acquire_vision_slot(v_conf):
            return None
        req_timeout = self._vision_request_timeout(v_conf)
        try:
            prompt_text = self._build_vision_route_prompt()
            url, headers, payload = self._build_vision_llm_request(
                images, v_conf, prompt_override=prompt_text, want_json=True
            )
            trust_env = not bool(v_conf.get("bypass_proxy", False))
            async with aiohttp.ClientSession(trust_env=trust_env) as session:
                async with self._post_vision_request(
                    session, url, headers, payload, v_conf
                ) as resp:
                    if resp.status == 200:
                        data = None
                        content = ""
                        try:
                            data = await resp.json()
                        except Exception:
                            text = await resp.text()
                            if text:
                                try:
                                    data = json.loads(text)
                                except Exception:
                                    content = text.strip()
                        if not content and isinstance(data, dict):
                            content = str(self._extract_vision_text(data, v_conf) or "").strip()
                        if not content:
                            logger.warning(
                                f"[vision_media] Vision route LLM returned empty json. Data: {data}"
                            )
                            self._record_vision_failure(v_conf, "empty route response")
                            return None
                        obj = self._extract_json_obj(content)
                        if not isinstance(obj, dict):
                            logger.warning(
                                f"[vision_media] Vision route json parse failed. Content: {content}"
                            )
                            self._record_vision_failure(v_conf, "invalid route JSON")
                            return None
                        route = str(obj.get("route") or "").strip().lower()
                        desc = re.sub(r"\s+", " ", str(obj.get("desc") or "")).strip()
                        if len(desc) > 80:
                            desc = desc[:80] + "..."
                        logger.info(
                            "[vision_media] Vision route success: route=%s anime_score=%s confidence=%s need_heavy=%s desc=%s",
                            route,
                            obj.get("anime_score"),
                            obj.get("confidence"),
                            obj.get("need_heavy"),
                            desc,
                        )
                        self._record_vision_success()
                        return obj
                    text = await resp.text()
                    if isinstance(payload, dict) and "response_format" in payload:
                        payload2 = dict(payload)
                        payload2.pop("response_format", None)
                        async with self._post_vision_request(
                            session, url, headers, payload2, v_conf
                        ) as resp2:
                            if resp2.status == 200:
                                data2 = await resp2.json()
                                content2 = str(
                                    self._extract_vision_text(data2, v_conf) or ""
                                ).strip()
                                obj2 = self._extract_json_obj(content2)
                                if isinstance(obj2, dict):
                                    self._record_vision_success()
                                    return obj2
                            text2 = await resp2.text()
                            self._record_vision_failure(
                                v_conf,
                                f"HTTP {resp.status}/{resp2.status}",
                            )
                            logger.error(
                                f"[vision_media] Vision route json failed: {resp2.status} - {text2[:500]}"
                            )
                    else:
                        self._record_vision_failure(v_conf, f"HTTP {resp.status}")
                        logger.error(
                            f"[vision_media] Vision route json failed: {resp.status} - {text[:500]}"
                        )
        except asyncio.TimeoutError:
            self._record_vision_failure(
                v_conf,
                f"timeout after {req_timeout:.1f}s",
            )
            logger.warning(
                "[vision_media] 视觉路由请求超过 %.1f 秒，已取消并回退",
                req_timeout,
            )
        except Exception as e:
            self._record_vision_failure(v_conf, type(e).__name__)
            logger.error(f"[vision_media] Vision route error: {e}")
        finally:
            self._vision_semaphore.release()
        return None

    async def extract_images_from_event(self, event: AstrMessageEvent) -> list[str]:
        images = []
        try:
            if (
                hasattr(event, "message_obj")
                and event.message_obj
                and hasattr(event.message_obj, "message")
            ):
                chain = event.message_obj.message
                for i in chain:
                    if isinstance(i, Image):
                        img_src = i.url if i.url else i.file
                        if img_src:
                            if img_src.startswith("`") and img_src.endswith("`"):
                                img_src = img_src.strip("`").strip()
                            images.append(img_src)
        except Exception as e:
            logger.error(f"[vision_media] Error extracting images: {e}")
        return images

    async def get_text_with_images(self, event: AstrMessageEvent) -> str:
        try:
            if (
                hasattr(event, "message_obj")
                and event.message_obj
                and hasattr(event.message_obj, "message")
            ):
                chain = event.message_obj.message
                if not chain:
                    return event.message_str or ""

                parts = []
                is_at = bool(getattr(event, "is_at_or_wake_command", False))
                group_id = str(event.get_group_id() or "").strip()
                sender_id = str(event.get_sender_id() or "").strip() or None
                msg_id = None
                try:
                    msg_obj = getattr(event, "message_obj", None)
                    if msg_obj:
                        msg_id = getattr(msg_obj, "message_id", None)
                except Exception:
                    msg_id = None

                for i in chain:
                    if isinstance(i, Plain):
                        parts.append(i.text)
                    elif isinstance(i, Image):
                        v_conf = self._vision_llm_config()

                        img_src = i.url if i.url else i.file
                        url = i.url
                        file_token = i.file

                        is_command = False
                        plain_text_content = "".join(
                            [x.text for x in chain if isinstance(x, Plain)]
                        ).strip()
                        ignore_prefixes = (
                            "#",
                            "/",
                            "＃",
                            "bnn",
                            "手办化",
                            "贴纸化",
                            "棉花娃娃化",
                            "服装分解",
                            "模型套件",
                            "角色界面",
                            "角色设定",
                            "钥匙扣",
                            "姿势表",
                            "转身动画",
                            "多机位",
                            "电影分镜",
                            "动漫分镜",
                            "真人化",
                            "场景真人化",
                            "半真人半动漫",
                            "元素融合",
                            "猫娘化",
                            "coser化",
                            "痛房化",
                            "痛车化",
                            "地雷女化",
                        )
                        if plain_text_content.startswith(ignore_prefixes):
                            is_command = True

                        chat_model_supports_vision = self.config.get(
                            "chat_model_supports_vision", False
                        )
                        should_run_vision = False

                        if v_conf.get("enable", False) and not is_command and img_src:
                            if is_at:
                                if not chat_model_supports_vision:
                                    should_run_vision = True
                            else:
                                should_run_vision = True

                        image_key = None
                        perceptual_key = None
                        cached_caption = None
                        claimed_caption_gen = False
                        mime_hint = None
                        cached_score = 0
                        _cj = None
                        precomputed_b64_frames = None

                        if self._image_cache and not is_command and img_src:
                            try:
                                image_key, _mime = self._image_cache.record_seen(
                                    group_id=group_id,
                                    url=url,
                                    file_token=file_token,
                                    sender_id=sender_id,
                                    msg_id=msg_id,
                                    is_at=is_at,
                                )
                                mime_hint = _mime
                            except Exception:
                                image_key = None

                            if not is_at and image_key:
                                try:
                                    cached_caption, cached_score, _cj = (
                                        self._image_cache.get_caption_meta(
                                            group_id=group_id, image_key=image_key
                                        )
                                    )
                                except Exception:
                                    cached_caption = None

                            if (
                                (not is_at)
                                and (not cached_caption)
                                and self._vision_simhash_enabled()
                                and img_src
                            ):
                                try:
                                    precomputed_b64_frames = await self.image_to_base64_frames(
                                        img_src
                                    )
                                except Exception:
                                    precomputed_b64_frames = None
                                if precomputed_b64_frames:
                                    try:
                                        perceptual_key = self._dhash64_from_data_url(
                                            precomputed_b64_frames[0]
                                        )
                                    except Exception:
                                        perceptual_key = None
                                if perceptual_key:
                                    try:
                                        self._image_cache.record_seen(
                                            group_id=group_id,
                                            url=url,
                                            file_token=perceptual_key,
                                            sender_id=sender_id,
                                            msg_id=msg_id,
                                            is_at=is_at,
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        cached_caption, cached_score, _cj = (
                                            self._image_cache.get_caption_meta(
                                                group_id=group_id,
                                                image_key=perceptual_key,
                                            )
                                        )
                                    except Exception:
                                        cached_caption = None
                                    if not cached_caption:
                                        try:
                                            (
                                                cached_caption,
                                                cached_score,
                                                _cj,
                                                _matched_key,
                                                _dist,
                                            ) = self._image_cache.find_similar_caption_meta(
                                                group_id=group_id,
                                                image_key=perceptual_key,
                                                max_hamming=self._vision_simhash_max_hamming(),
                                                scan_limit=self._vision_simhash_scan_limit(),
                                            )
                                            if (
                                                cached_caption
                                                and self._vision_simhash_ui_strict_enable()
                                                and self._looks_like_ui_screenshot_caption(
                                                    cached_caption
                                                )
                                                and int(_dist or 64)
                                                > self._vision_simhash_ui_max_hamming()
                                            ):
                                                logger.info(
                                                    "[vision_media] skip simhash near-hit for ui-like screenshot caption (dist=%s > ui_max=%s)",
                                                    _dist,
                                                    self._vision_simhash_ui_max_hamming(),
                                                )
                                                cached_caption = None
                                        except Exception:
                                            cached_caption = None

                        if cached_caption:
                            cached_obj = None
                            if _cj:
                                try:
                                    cached_obj = self._extract_json_obj(_cj)
                                except Exception:
                                    cached_obj = None
                            nsfw_hit, violation_text, nsfw_score, reason_text = (
                                self._extract_nsfw_info(cached_obj)
                                if cached_obj
                                else (False, None, 0, None)
                            )
                            if nsfw_hit:
                                mark_text = self._vision_nsfw_mark_text()
                                out = f"[图片内容: {mark_text}]"
                                if (
                                    self._vision_nsfw_withdraw_enabled()
                                    and msg_id is not None
                                ):
                                    await self._try_withdraw_msg(event, str(msg_id))
                                if self._image_cache and image_key and not is_at:
                                    try:
                                        self._image_cache.set_caption(
                                            group_id=group_id,
                                            image_key=image_key,
                                            caption=mark_text,
                                            caption_json=_cj,
                                            essence_flag=0,
                                            essence_score=0,
                                            model=v_conf.get("model") if v_conf else None,
                                            status="ready",
                                            err=None,
                                        )
                                    except Exception:
                                        pass
                                parts.append(out)
                                continue
                            out = f"[图片内容: {cached_caption}]"
                            try:
                                sc = int(cached_score or 0)
                            except Exception:
                                sc = 0
                            if sc >= self._vision_essence_score_threshold():
                                out = out + f"[{self._vision_marker_name()}=1]"
                                await self._maybe_set_essence_for_image(
                                    event=event,
                                    group_id=group_id,
                                    image_key=perceptual_key or image_key,
                                    msg_id=str(msg_id) if msg_id is not None else None,
                                    score=sc,
                                )
                            parts.append(out)
                            if self._image_cache and image_key and not is_at:
                                try:
                                    self._image_cache.set_caption(
                                        group_id=group_id,
                                        image_key=image_key,
                                        caption=cached_caption,
                                        caption_json=_cj,
                                        essence_flag=1
                                        if sc >= self._vision_essence_score_threshold()
                                        else 0,
                                        essence_score=int(sc or 0),
                                        model=v_conf.get("model") if v_conf else None,
                                        status="ready",
                                        err=None,
                                    )
                                except Exception:
                                    pass
                            continue

                        if should_run_vision:
                            if self._image_cache and image_key and not is_at:
                                try:
                                    claimed_caption_gen = (
                                        self._image_cache.claim_caption_generation(
                                            group_id=group_id, image_key=image_key
                                        )
                                    )
                                    if not claimed_caption_gen:
                                        waited = self._image_cache.wait_caption_ready(
                                            group_id=group_id,
                                            image_key=image_key,
                                            timeout_sec=1.0,
                                            interval_sec=0.2,
                                        )
                                        if waited:
                                            cap2, sc2, _cj2 = (
                                                self._image_cache.get_caption_meta(
                                                    group_id=group_id,
                                                    image_key=image_key,
                                                )
                                            )
                                            out = f"[图片内容: {cap2 or waited}]"
                                            try:
                                                scv = int(sc2 or 0)
                                            except Exception:
                                                scv = 0
                                            if scv >= self._vision_essence_score_threshold():
                                                out = out + f"[{self._vision_marker_name()}=1]"
                                                await self._maybe_set_essence_for_image(
                                                    event=event,
                                                    group_id=group_id,
                                                    image_key=image_key,
                                                    msg_id=str(msg_id)
                                                    if msg_id is not None
                                                    else None,
                                                    score=scv,
                                                )
                                            parts.append(out)
                                            continue
                                except Exception:
                                    claimed_caption_gen = False

                            b64_frames = precomputed_b64_frames
                            if not b64_frames:
                                b64_frames = await self.image_to_base64_frames(img_src)
                            if not b64_frames:
                                parts.append("[图片]")
                                continue
                            if not perceptual_key and self._vision_simhash_enabled():
                                try:
                                    perceptual_key = self._dhash64_from_data_url(
                                        b64_frames[0]
                                    )
                                except Exception:
                                    perceptual_key = None

                            response_mode = self._vision_response_mode()
                            desc = ""
                            score = 0
                            cj = None
                            model_for_cache = v_conf.get("model")
                            if self._vision_route_should_try_fast(
                                plain_text_content=plain_text_content,
                                img_src=img_src,
                                file_token=file_token,
                                v_conf=v_conf,
                            ):
                                try:
                                    fast_conf = self._vision_fast_llm_config(v_conf)
                                    route_imgs = self._prepare_vision_route_images(
                                        b64_frames
                                    )
                                    route_obj = (
                                        await self.call_vision_route_llm(
                                            route_imgs, fast_conf
                                        )
                                        if fast_conf and route_imgs
                                        else None
                                    )
                                    route_decision = self._vision_route_decision(
                                        route_obj
                                    )
                                    if (
                                        route_decision == "simple"
                                        and isinstance(route_obj, dict)
                                    ):
                                        desc = str(route_obj.get("desc") or "").strip()
                                        if len(desc) > 160:
                                            desc = desc[:160] + "..."
                                        score = 0
                                        cj = self._vision_route_caption_json(
                                            route_obj, desc
                                        )
                                        model_name = (
                                            str(fast_conf.get("model") or "").strip()
                                            if fast_conf
                                            else ""
                                        )
                                        model_for_cache = (
                                            f"fast:{model_name}"
                                            if model_name
                                            else "fast"
                                        )
                                        logger.info(
                                            "[vision_media] Vision route simple hit; skip heavy vision model=%s desc=%s",
                                            model_for_cache,
                                            desc,
                                        )
                                    else:
                                        logger.info(
                                            "[vision_media] Vision route escalated to heavy decision=%s",
                                            route_decision,
                                        )
                                except Exception as e:
                                    logger.warning(
                                        f"[vision_media] Vision route failed; fallback to heavy: {e}"
                                    )
                            if (
                                not self._warned_essence_requires_json
                                and self._vision_essence_enabled()
                                and response_mode != "json"
                            ):
                                self._warned_essence_requires_json = True
                                logger.warning(
                                    "[vision_media] vision_essence_enable=true but vision_llm_response_mode!=json; essence 标记与自动精华将不会生效"
                                )
                            if not desc and response_mode == "json":
                                obj = await self.call_vision_llm_json(b64_frames, v_conf)
                                if isinstance(obj, dict):
                                    desc = str(obj.get("desc") or "").strip()
                                    try:
                                        score = int(obj.get("score") or 0)
                                    except Exception:
                                        score = 0
                                    if not obj.get("score") and "essence" in obj:
                                        try:
                                            score = 100 if int(obj.get("essence") or 0) == 1 else 0
                                        except Exception:
                                            score = 0
                                    score = max(0, min(100, score))
                                    nsfw_hit, violation_text, nsfw_score, reason_text = (
                                        self._extract_nsfw_info(obj)
                                    )
                                    if nsfw_hit:
                                        desc = self._vision_nsfw_mark_text()
                                        score = 0
                                    if nsfw_hit and not violation_text:
                                        violation_text = "黄暴图片"
                                    try:
                                        cj = json.dumps(
                                            {
                                                "desc": desc,
                                                "score": score,
                                                "nsfw_score": nsfw_score,
                                                "violation": violation_text or "",
                                                "reason": reason_text or "",
                                            },
                                            ensure_ascii=False,
                                        )
                                    except Exception:
                                        cj = None
                                    if (
                                        nsfw_hit
                                        and self._vision_nsfw_withdraw_enabled()
                                        and msg_id is not None
                                    ):
                                        await self._try_withdraw_msg(event, str(msg_id))
                            elif not desc:
                                caption = await self.call_vision_llm(b64_frames, v_conf)
                                if caption:
                                    desc = (
                                        caption.replace("[图片内容:", "")
                                        .replace("]", "")
                                        .strip()
                                    )

                            if desc:
                                out = f"[图片内容: {desc}]"
                                essence_flag = (
                                    1
                                    if score >= self._vision_essence_score_threshold()
                                    else 0
                                )
                                if (
                                    desc == self._vision_nsfw_mark_text()
                                    and self._vision_nsfw_enabled()
                                ):
                                    essence_flag = 0
                                if essence_flag == 1:
                                    out = out + f"[{self._vision_marker_name()}=1]"
                                    await self._maybe_set_essence_for_image(
                                        event=event,
                                        group_id=group_id,
                                        image_key=perceptual_key or image_key,
                                        msg_id=str(msg_id) if msg_id is not None else None,
                                        score=int(score or 0),
                                    )
                                parts.append(out)
                                if self._image_cache and (image_key or perceptual_key) and not is_at:
                                    try:
                                        cache_keys = []
                                        if image_key:
                                            cache_keys.append(image_key)
                                        if perceptual_key and perceptual_key not in cache_keys:
                                            cache_keys.append(perceptual_key)
                                        # 从 perceptual_key 取出 dhash64 hex，写入主键行的 dhash64 侧列
                                        _dh_hex = None
                                        if perceptual_key and str(perceptual_key).startswith("dhash64:"):
                                            _dh_hex = str(perceptual_key).split(":", 1)[1].strip().lower() or None
                                        for ck in cache_keys:
                                            self._image_cache.set_caption(
                                                group_id=group_id,
                                                image_key=ck,
                                                caption=desc,
                                                caption_json=cj,
                                                essence_flag=essence_flag,
                                                essence_score=int(score or 0),
                                                model=model_for_cache,
                                                status="ready",
                                                err=None,
                                                dhash64_hex=_dh_hex,
                                            )
                                    except Exception:
                                        pass
                            else:
                                fallback_text = self._vision_failure_fallback_text(
                                    v_conf
                                )
                                parts.append(f"[图片内容: {fallback_text}]")
                                if self._image_cache and (image_key or perceptual_key) and not is_at:
                                    try:
                                        cache_keys = []
                                        if image_key:
                                            cache_keys.append(image_key)
                                        if perceptual_key and perceptual_key not in cache_keys:
                                            cache_keys.append(perceptual_key)
                                        _dh_hex = None
                                        if perceptual_key and str(perceptual_key).startswith("dhash64:"):
                                            _dh_hex = str(perceptual_key).split(":", 1)[1].strip().lower() or None
                                        for ck in cache_keys:
                                            self._image_cache.set_caption(
                                                group_id=group_id,
                                                image_key=ck,
                                                caption="",
                                                caption_json=cj,
                                                essence_flag=0,
                                                essence_score=int(score or 0),
                                                model=model_for_cache,
                                                status="error",
                                                err="vision llm unavailable, timed out, or returned empty",
                                                dhash64_hex=_dh_hex,
                                            )
                                    except Exception:
                                        pass
                            continue

                        parts.append("[图片]")
                        if self._image_cache and image_key and not is_at:
                            try:
                                self._image_cache.set_caption(
                                    group_id=group_id,
                                    image_key=image_key,
                                    caption="",
                                    caption_json=None,
                                    essence_flag=0,
                                    model=v_conf.get("model") if v_conf else None,
                                    status="ready",
                                    err=None,
                                )
                            except Exception:
                                pass
                    elif isinstance(i, Face):
                        try:
                            parts.append(f"[表情:{getattr(i, 'id', '')}]")
                        except Exception:
                            parts.append("[表情]")
                    elif isinstance(i, At):
                        try:
                            parts.append(f"@{getattr(i, 'qq', '')} ")
                        except Exception:
                            parts.append("@ ")
                    elif isinstance(i, AtAll):
                        parts.append("@全体成员 ")
                    elif isinstance(i, Reply):
                        parts.append("[回复]")
                    elif isinstance(i, Poke):
                        parts.append("[戳一戳]")
                    else:
                        try:
                            parts.append(str(i))
                        except Exception:
                            pass

                text = "".join(parts).strip()
                return text or (event.message_str or "")
        except Exception as e:
            logger.error(f"[vision_media] Error in get_text_with_images: {e}")

        return event.message_str or ""


__all__ = ["MediaManager"]
