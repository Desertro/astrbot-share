# main.py

import asyncio
import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Json,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    VideoContent,
)
from .core.debounce import LinkDebouncer
from .core.download import Downloader
from .core.exception import (
    DownloadException,
    DownloadLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .core.parsers import (
    BaseParser,
    BilibiliParser,
    YouTubeParser,
)
from .core.render import Renderer
from .core.utils import (
    encode_gif_to_mp4,
    encode_video_to_gif,
    extract_json_url,
    save_cookies_with_netscape,
)


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._recent_image_keys: dict[str, dict[str, float]] = {}
        self._image_key_cache: dict[str, tuple[float, str]] = {}

        # 插件数据目录
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_parser")
        config["data_dir"] = str(self.data_dir)

        # 缓存目录
        self.cache_dir: Path = self.data_dir / "cache_dir"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config["cache_dir"] = str(self.cache_dir)
        self.config.save_config()

        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}

        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

        # 渲染器
        self.renderer = Renderer(config)

        # 下载器
        self.downloader = Downloader(config)

        # 防抖器
        self.debouncer = LinkDebouncer(config)

        # 仲裁器
        self.arbiter = EmojiLikeArbiter()

        # 缓存清理器
        self.cleaner = CacheCleaner(self.context, self.config)

    # region 生命周期

    async def initialize(self):
        """加载、重载插件时触发"""
        # ytb_cookies
        if self.config["ytb_ck"]:
            ytb_cookies_file = self.data_dir / "ytb_cookies.txt"
            ytb_cookies_file.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                save_cookies_with_netscape,
                self.config["ytb_ck"],
                ytb_cookies_file,
                "youtube.com",
            )
            self.config["ytb_cookies_file"] = str(ytb_cookies_file)
            self.config.save_config()
        # 加载资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器"""
        # 获取所有解析器
        all_subclass = BaseParser.get_all_subclass()
        # 过滤掉禁用的平台
        enabled_classes = [
            _cls
            for _cls in all_subclass
            if _cls.platform.display_name in self.config["enable_platforms"]
        ]
        # 启用的平台
        platform_names = []
        for _cls in enabled_classes:
            parser = _cls(self.config, self.downloader)
            platform_names.append(parser.platform.display_name)
            for keyword, _ in _cls._key_patterns:
                self.parser_map[keyword] = parser
        logger.info(f"启用平台: {'、'.join(platform_names)}")

        # 关键词-正则对，一次性生成并排序
        patterns: list[tuple[str, re.Pattern[str]]] = [
            (kw, re.compile(pt) if isinstance(pt, str) else pt)
            for cls in enabled_classes
            for kw, pt in cls._key_patterns
        ]
        # 长关键词优先
        patterns.sort(key=lambda x: -len(x[0]))
        keywords = [kw for kw, _ in patterns]
        logger.debug(f"关键词-正则对已生成：{keywords}")
        self.key_pattern_list = patterns

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    @staticmethod
    def _is_gif_path(path: Path | str) -> bool:
        return Path(path).suffix.lower() == ".gif"

    def _should_send_gif_as_video(self, path: Path) -> bool:
        threshold_mb = int(
            self.config.get("dynamic_gif_to_video_threshold_mb", 50) or 0
        )
        if threshold_mb <= 0 or not path.exists():
            return False
        return path.stat().st_size >= threshold_mb * 1024 * 1024

    async def _resolve_dynamic_send_path(
        self, cont: DynamicContent, path: Path
    ) -> Path:
        if self._is_gif_path(path):
            return path
        if cont.gif_path and cont.gif_path.exists():
            return cont.gif_path
        cont.gif_path = await encode_video_to_gif(path)
        return cont.gif_path

    async def _build_dynamic_component(
        self, cont: DynamicContent, path: Path
    ) -> BaseMessageComponent:
        send_path = await self._resolve_dynamic_send_path(cont, path)
        if self._is_gif_path(send_path):
            if self._should_send_gif_as_video(send_path):
                send_path = await encode_gif_to_mp4(
                    send_path,
                    ffmpeg_threads=int(self.config.get("ffmpeg_max_threads", 1) or 1),
                )
                return Video(str(send_path))
            return Image(str(send_path))
        return Video(str(send_path))

    def _build_send_plan(self, result: ParseResult):
        light_contents = []
        heavy_contents = []

        for cont in chain(
            result.contents, result.repost.contents if result.repost else ()
        ):
            match cont:
                case ImageContent() | GraphicsContent() | DynamicContent():
                    light_contents.append(cont)
                case VideoContent() | AudioContent() | FileContent():
                    heavy_contents.append(cont)
                case _:
                    light_contents.append(cont)

        heavy_count = len(heavy_contents)
        light_count = len(light_contents)

        base_seg_count = heavy_count + light_count
        pure_light_batch = heavy_count == 0 and light_count > 0

        is_single_heavy = heavy_count == 1 and light_count == 0

        render_card = is_single_heavy and self.config.get(
            "single_heavy_render_card", False
        )

        final_seg_count = base_seg_count + (1 if render_card else 0)
        # NapCat 当前对合并转发的稳定性较差，纯图片批次优先走普通发送。
        force_merge = (
            final_seg_count >= self.config["forward_threshold"] and not pure_light_batch
        )

        return {
            "light": light_contents,
            "heavy": heavy_contents,
            "render_card": render_card,
            "force_merge": force_merge,
            "preview_card": render_card and not force_merge,
        }

    async def _send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
    ):
        plan = self._build_send_plan(result)
        send_timeout = 25.0

        segs: list[BaseMessageComponent] = []
        fallback_segs: list[BaseMessageComponent] | None = None
        show_download_fail_tip = self.config.get("show_download_fail_tip", True)
        umo = getattr(event, "unified_msg_origin", "") or ""
        seen_image_keys: set[str] = set()
        debounce_interval = int(self.config.get("debounce_interval", 0) or 0)

        def _hit_recent_image_key(session: str, key: str) -> bool:
            if not debounce_interval or not session or not key:
                return False
            now = time.time()
            bucket = self._recent_image_keys.setdefault(session, {})
            for k, ts in list(bucket.items()):
                if now - ts > debounce_interval:
                    bucket.pop(k, None)
            if key in bucket:
                return True
            bucket[key] = now
            return False

        def _compute_key_sync(path_str: str) -> str:
            try:
                p = str(path_str or "").strip()
                if not p:
                    return ""
                if p.startswith("http"):
                    return p
                if not os.path.exists(p):
                    return p
                st = os.stat(p)
                mtime = float(st.st_mtime)
                cached = self._image_key_cache.get(p)
                if cached and cached[0] == mtime:
                    return cached[1]
                h = hashlib.blake2b(digest_size=16)
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 64), b""):
                        h.update(chunk)
                key = h.hexdigest()
                self._image_key_cache[p] = (mtime, key)
                return key
            except Exception:
                return str(path_str or "").strip()

        async def _image_key(path_str: str) -> str:
            return await asyncio.to_thread(_compute_key_sync, path_str)

        async def _send_with_timeout(message_chain, status="sent"):
            delivered = False
            try:
                result_value = await asyncio.wait_for(
                    event.send(message_chain), timeout=send_timeout
                )
                delivered = True
                return result_value
            finally:
                # Direct sends bypass AstrBot's respond-stage hooks.
                try:
                    meta = self.context.get_registered_star("mem_chat")
                    target = (
                        getattr(meta, "star_cls", None)
                        if meta and getattr(meta, "activated", True)
                        else None
                    )
                    if target and hasattr(target, "record_plugin_activity"):
                        await target.record_plugin_activity(
                            event,
                            plugin="parser",
                            status=status if delivered else "send_failed",
                            summary=str(result.title or "链接解析结果")
                            if delivered
                            else "链接已解析，但这次发送失败。",
                            source_url=str(result.url or ""),
                            chain=message_chain if delivered else None,
                        )
                except Exception as exc:
                    logger.warning(
                        "[parser] Failed to publish plugin activity: %s",
                        type(exc).__name__,
                    )

        def _build_timeout_tip() -> str:
            parts = []
            if result.title:
                parts.append(f"解析成功：{result.title}")
            else:
                parts.append("解析成功")
            if result.url:
                parts.append(result.url)
            parts.append("但视频发送超时，请检查 OneBot/NapCat 的视频发送能力。")
            return "\n".join(parts)

        # 预览卡片（单重媒体 + 不合并）
        if plan["preview_card"]:
            if image_path := await self.renderer.render_card(result):
                k = await _image_key(str(image_path))
                if k and not _hit_recent_image_key(umo, k):
                    seen_image_keys.add(k)
                    await _send_with_timeout(
                        event.chain_result([Image(str(image_path))])
                    )

        # inline 卡片（合并时）
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                k = await _image_key(str(image_path))
                if k and not _hit_recent_image_key(umo, k) and k not in seen_image_keys:
                    seen_image_keys.add(k)
                    segs.append(Image(str(image_path)))

        # 轻媒体
        for cont in plan["light"]:
            try:
                logger.debug(f"[parser] 开始解析轻媒体路径: {cont!r}")
                path: Path = await cont.get_path()
                logger.debug(f"[parser] 轻媒体路径就绪: {path}")
            except (DownloadLimitException, ZeroSizeException) as e:
                logger.warning(f"[parser] 轻媒体下载被跳过: {e}")
                if show_download_fail_tip:
                    segs.append(Plain(str(e)))
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    k = await _image_key(str(path))
                    if (
                        k
                        and not _hit_recent_image_key(umo, k)
                        and k not in seen_image_keys
                    ):
                        seen_image_keys.add(k)
                        segs.append(Image(str(path)))
                case DynamicContent():
                    segs.append(await self._build_dynamic_component(cont, path))
                case GraphicsContent() as g:
                    k = await _image_key(str(path))
                    if (
                        k
                        and not _hit_recent_image_key(umo, k)
                        and k not in seen_image_keys
                    ):
                        seen_image_keys.add(k)
                        segs.append(Image(str(path)))
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体
        for cont in plan["heavy"]:
            try:
                logger.debug(f"[parser] 开始解析重媒体路径: {cont!r}")
                path: Path = await cont.get_path()
                logger.debug(f"[parser] 重媒体路径就绪: {path}")
            except SizeLimitException:
                if show_download_fail_tip:
                    segs.append(Plain("超过文件大小限制"))
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent():
                    segs.append(Video(str(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=str(path))
                        if self.config["audio_to_file"]
                        else Record(str(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=str(path)))

        # 强制合并
        if plan["force_merge"] and segs:
            fallback_segs = list(segs)
            nodes = Nodes([])
            self_id = event.get_self_id()
            for seg in segs:
                nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))
            segs = [nodes]

        # 发送
        if segs:
            logger.debug(
                f"[parser] 准备发送解析结果: seg_count={len(segs)} force_merge={plan['force_merge']}"
            )
            try:
                await _send_with_timeout(event.chain_result(segs))
            except asyncio.TimeoutError:
                logger.warning("[parser] 解析结果发送超时，回退为纯文本提示")
                try:
                    await _send_with_timeout(
                        event.plain_result(_build_timeout_tip()), status="send_failed"
                    )
                except Exception as tip_exc:
                    logger.warning(f"[parser] 纯文本超时提示发送失败: {tip_exc}")
            except Exception as e:
                if fallback_segs:
                    logger.warning(
                        f"[parser] 合并转发发送失败，回退为普通消息发送: {e}"
                    )
                    try:
                        await _send_with_timeout(event.chain_result(fallback_segs))
                        return
                    except Exception as fallback_exc:
                        logger.warning(
                            f"[parser] 普通消息回退发送也失败: {fallback_exc}"
                        )
                else:
                    logger.warning(f"[parser] 解析结果发送失败: {e}")
                try:
                    await _send_with_timeout(
                        event.plain_result(
                            "解析成功，但图片发送失败，请检查 OneBot/NapCat 的图片发送能力。"
                        ),
                        status="send_failed",
                    )
                except Exception:
                    pass

    async def parse_url_to_components(self, url: str, session: str | None = None):
        """解析单个链接并返回消息组件列表，不直接发送
        Args:
            url: 待解析的链接
            session: 可选，会话标识，用于轻量的图片去重场景
        Returns:
            list[BaseMessageComponent]: 解析得到的消息组件
        """
        # 确保解析器已注册
        if not self.key_pattern_list or not self.parser_map:
            self._register_parser()

        # 关键词 + 正则匹配
        keyword: str = ""
        searched = None
        for kw, pat in self.key_pattern_list:
            if kw not in url:
                continue
            m = pat.search(url)
            if m:
                keyword, searched = kw, m
                break
        if searched is None:
            return []

        # 执行解析
        try:
            result = await self.parser_map[keyword].parse(keyword, searched)
        except ParseException as e:
            logger.warning(f"[parser] 解析失败: {e}")
            return []
        plan = self._build_send_plan(result)

        segs: list[BaseMessageComponent] = []
        show_download_fail_tip = self.config.get("show_download_fail_tip", True)
        umo = session or ""
        seen_image_keys: set[str] = set()
        debounce_interval = int(self.config.get("debounce_interval", 0) or 0)

        def _hit_recent_image_key(sess: str, key: str) -> bool:
            if not debounce_interval or not sess or not key:
                return False
            now = time.time()
            bucket = self._recent_image_keys.setdefault(sess, {})
            for k, ts in list(bucket.items()):
                if now - ts > debounce_interval:
                    bucket.pop(k, None)
            if key in bucket:
                return True
            bucket[key] = now
            return False

        def _compute_key_sync(path_str: str) -> str:
            try:
                p = str(path_str or "").strip()
                if not p:
                    return ""
                if p.startswith("http"):
                    return p
                if not os.path.exists(p):
                    return p
                st = os.stat(p)
                mtime = float(st.st_mtime)
                cached = self._image_key_cache.get(p)
                if cached and cached[0] == mtime:
                    return cached[1]
                h = hashlib.blake2b(digest_size=16)
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 64), b""):
                        h.update(chunk)
                key = h.hexdigest()
                self._image_key_cache[p] = (mtime, key)
                return key
            except Exception:
                return str(path_str or "").strip()

        async def _image_key(path_str: str) -> str:
            return await asyncio.to_thread(_compute_key_sync, path_str)

        # 预览卡片（单重媒体 + 不合并）
        if plan["preview_card"]:
            image_path = await self.renderer.render_card(result)
            if image_path:
                k = await _image_key(str(image_path))
                if k and not _hit_recent_image_key(umo, k):
                    seen_image_keys.add(k)
                    segs.append(Image(str(image_path)))

        # inline 卡片（合并时）
        if plan["render_card"] and plan["force_merge"]:
            image_path = await self.renderer.render_card(result)
            if image_path:
                k = await _image_key(str(image_path))
                if k and not _hit_recent_image_key(umo, k) and k not in seen_image_keys:
                    seen_image_keys.add(k)
                    segs.append(Image(str(image_path)))

        # 轻媒体
        for cont in plan["light"]:
            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    k = await _image_key(str(path))
                    if (
                        k
                        and not _hit_recent_image_key(umo, k)
                        and k not in seen_image_keys
                    ):
                        seen_image_keys.add(k)
                        segs.append(Image(str(path)))
                case DynamicContent():
                    segs.append(await self._build_dynamic_component(cont, path))
                case GraphicsContent() as g:
                    k = await _image_key(str(path))
                    if (
                        k
                        and not _hit_recent_image_key(umo, k)
                        and k not in seen_image_keys
                    ):
                        seen_image_keys.add(k)
                        segs.append(Image(str(path)))
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                if show_download_fail_tip:
                    segs.append(Plain("超过文件大小限制"))
                continue
            except DownloadException:
                if show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent():
                    segs.append(Video(str(path)))
                case AudioContent():
                    segs.append(
                        File(name=path.name, file=str(path))
                        if self.config["audio_to_file"]
                        else Record(str(path))
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=str(path)))

        return segs

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        # 忽略自己发送的消息
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        umo = event.unified_msg_origin

        # 禁用会话
        if umo in self.config["disabled_sessions"]:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 卡片解析：解析Json组件，提取URL
        if isinstance(seg1, Json):
            text = extract_json_url(seg1.data)
            logger.debug(f"解析Json组件: {text}")

        if not text:
            return

        self_id = event.get_self_id()

        # 指定机制：专门@其他bot的消息不解析
        if isinstance(seg1, At) and str(seg1.qq) != self_id:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 防抖机制：避免短时间重复处理同一链接
        link = searched.group(0)
        link_for_debounce = (
            link.strip().strip("`").strip().rstrip(")】]>》。，；,.;!！?？\"'")
        )
        if "?" in link_for_debounce or "#" in link_for_debounce:
            try:
                from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

                parts = urlsplit(link_for_debounce)
                if parts.scheme in {"http", "https"}:
                    drop_keys = {
                        "click_from",
                        "utm_source",
                        "utm_medium",
                        "utm_campaign",
                        "utm_term",
                        "utm_content",
                    }
                    q = [
                        (k, v)
                        for k, v in parse_qsl(parts.query, keep_blank_values=True)
                        if k not in drop_keys
                    ]
                    link_for_debounce = urlunsplit(
                        (
                            parts.scheme,
                            parts.netloc,
                            parts.path,
                            urlencode(q, doseq=True),
                            "",
                        )
                    )
            except Exception:
                link_for_debounce = link_for_debounce
        if self.config["debounce_interval"] and self.debouncer.hit(
            umo, link_for_debounce
        ):
            logger.warning(f"[防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        try:
            parse_res = await self.parser_map[keyword].parse(keyword, searched)
        except ParseException as e:
            logger.warning(f"[parser] 解析失败: {e}")
            error_text = str(e)
            if "Pixiv" in error_text and any(
                marker in error_text for marker in ("401", "403", "pixiv_ck", "登录")
            ):
                try:
                    await event.send(
                        event.plain_result(
                            "Pixiv 解析失败：登录 Cookie 已失效，或该作品需要登录。"
                            "请更新 pixiv_ck 后重试。"
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "[parser] Failed to send Pixiv auth error: %s",
                        type(exc).__name__,
                    )
            try:
                meta = self.context.get_registered_star("mem_chat")
                target = (
                    getattr(meta, "star_cls", None)
                    if meta and getattr(meta, "activated", True)
                    else None
                )
                if target and hasattr(target, "record_plugin_activity"):
                    await target.record_plugin_activity(
                        event,
                        plugin="parser",
                        status="failed",
                        summary="链接解析失败，本次没有得到可发送的内容。",
                        source_url=link,
                    )
            except Exception as exc:
                logger.warning(
                    "[parser] Failed to publish parse failure: %s", type(exc).__name__
                )
            return
        logger.debug(
            f"[parser] 解析完成: title={parse_res.title!r} contents={len(parse_res.contents)}"
        )

        # 发送
        await self._send_parse_result(event, parse_res)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bm")
    async def bm(self, event: AstrMessageEvent):
        """获取B站的音频"""
        text = event.message_str
        matched = re.search(r"(BV[A-Za-z0-9]{10})(\s\d{1,3})?", text)
        if not matched:
            yield event.plain_result("请发送正确的 BV 号")
            return

        bvid, page_num = matched.group(1), matched.group(2)
        page_idx = int(page_num) if page_num else 0

        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore

        _, audio_url = await parser.extract_download_urls(
            bvid=bvid, page_index=page_idx
        )
        if not audio_url:
            yield event.plain_result("未找到可下载的音频")
            return

        audio_path = await self.downloader.download_audio(
            audio_url,
            audio_name=f"{bvid}-{page_idx}.mp3",
            ext_headers=parser.headers,
            proxy=parser.proxy,
        )
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ym")
    async def ym(self, event: AstrMessageEvent):
        """获取油管的音频"""
        text = event.message_str
        parser = self._get_parser_by_type(YouTubeParser)
        _, matched = parser.search_url(text)
        if not matched:
            yield event.plain_result("请发送正确的油管链接")
            return

        url = matched.group(0)

        audio_path = await self.downloader.download_audio(
            url, use_ytdlp=True, proxy=parser.proxy
        )
        yield event.chain_result([Record(audio_path)])  # type: ignore

        if self.config["upload_audio"]:
            pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.check_qr_state():
            yield event.plain_result(msg)

    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        if umo in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].remove(umo)
            self.config.save_config()
            yield event.plain_result("解析已开启")
        else:
            yield event.plain_result("解析已开启，无需重复开启")

    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        if umo not in self.config["disabled_sessions"]:
            self.config["disabled_sessions"].append(umo)
            self.config.save_config()
            yield event.plain_result("解析已关闭")
        else:
            yield event.plain_result("解析已关闭，无需重复关闭")
