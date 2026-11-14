import base64
import random
import re
import shlex
from pathlib import Path

import aiohttp

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class ParamsCollector:
    """
    参数收集类
    """

    def __init__(self, config: AstrBotConfig):
        self.conf = config
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            trust_env=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )

    async def _download_image(self, url: str) -> bytes | None:
        """下载图片"""
        try:
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
        except Exception as e:
            logger.error(f"图片下载失败: {e}")
            return None

    async def get_avatar(self, user_id: str) -> bytes | None:
        """根据 QQ 号下载头像"""
        if not user_id.isdigit():
            user_id = f"{random.randint(10_000_000, 999_999_999)}"
        avatar_urls = [
            f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640",
            f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640",
        ]
        for avatar_url in avatar_urls:
            if avatar := await self._download_image(avatar_url):
                return avatar
        return None

    async def _decode_image(self, src: str) -> bytes | None:
        """统一把 src 转成 bytes"""
        raw: bytes | None = None
        # 1. 本地文件
        if Path(src).is_file():
            raw = Path(src).read_bytes()
        # 2. URL
        elif src.startswith("http"):
            raw = await self._download_image(src)
        # 3. Base64
        elif src.startswith("base64://"):
            return base64.b64decode(src[9:])
        # 4. 返回bytes/None
        return raw if isinstance(raw, bytes) else None

    async def get_extra(self, event: AstrMessageEvent, target_id: str):
        """从消息平台获取参数"""
        if event.get_platform_name() == "aiocqhttp":
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            assert isinstance(event, AiocqhttpMessageEvent)
            group_id = event.get_group_id()
            user_info = None

            if group_id:
                try:
                    user_info = await event.bot.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(target_id),
                        no_cache=False,
                    )
                except Exception as e:
                    logger.error(f"获取群成员信息失败: {e}")

            if not user_info:
                user_info = await event.bot.get_stranger_info(user_id=int(target_id))

            raw_nickname = user_info.get("card") or user_info.get("nickname")
            nickname = str(raw_nickname if raw_nickname is not None else "Unknown")
            sex = user_info.get("sex")
            return nickname, sex
        # TODO 适配更多消息平台

    async def _append_id_info(
        self,
        event: AstrMessageEvent,
        target_id: str,
        images: list,
        options: dict,
        prefer_front: bool = False,
    ):
        """补齐昵称、性别、头像信息"""
        nickname = str(target_id)
        sex = None
        if result := await self.get_extra(event, target_id):
            nickname, sex = result
        options["name"], options["gender"] = nickname, sex
        if avatar := await self.get_avatar(target_id):
            if prefer_front:
                images.insert(0, (nickname, avatar))
            else:
                images.append((nickname, avatar))

    def _option_aliases(self, params):
        aliases: dict[str, list[tuple[str, str]]] = {}
        args_type = getattr(params, "args_type", None)

        def add_alias(alias: str, args: list[tuple[str, str]]):
            if not alias:
                return
            aliases[alias] = args
            if alias.startswith("-"):
                stripped = alias.lstrip("-")
                if stripped:
                    aliases[stripped] = args

        model = getattr(args_type, "args_model", None)
        fields = getattr(model, "model_fields", None) or getattr(model, "__fields__", {})
        for name, field_info in fields.items():
            if name == "user_infos":
                continue
            annotation = getattr(field_info, "annotation", None) or getattr(
                field_info, "type_", str
            )
            value_type = getattr(annotation, "__name__", str(annotation))
            add_alias(name, [(name, value_type)])

        for option in getattr(args_type, "parser_options", []) or []:
            option_args = []
            for arg in option.args or []:
                option_args.append((arg.name, arg.value))
            if not option_args:
                dest = option.dest or option.names[-1].lstrip("-").replace("-", "_")
                option_args = [(dest, "bool")]
            elif option.dest and len(option_args) == 1:
                option_args = [(option.dest, option_args[0][1])]
            for name in option.names:
                add_alias(name, option_args)
        return aliases

    def _coerce_option_value(self, value: str, value_type: str):
        value_type = str(value_type).lower()
        if value_type in {"int", "integer"}:
            return int(value)
        if value_type in {"float", "number"}:
            return float(value)
        if value_type in {"bool", "boolean"}:
            return value.strip().lower() in {"1", "true", "yes", "on", "y", "是", "真"}
        return value

    def _split_plain_args(self, text: str) -> list[str]:
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()

    def _extract_plain_params(self, text: str, params):
        options: dict[str, bool | str | int | float] = {}
        text_tokens: list[str] = []
        aliases = self._option_aliases(params)
        tokens = self._split_plain_args(text)
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token:
                i += 1
                continue

            if re.fullmatch(r"[@＠]\d{1,13}", token):
                i += 1
                continue

            key = None
            value = None
            if "=" in token:
                raw_key, raw_value = token.split("=", 1)
                if raw_key in aliases or raw_key.lstrip("-") in aliases:
                    key, value = raw_key, raw_value
                elif not raw_key.startswith("-"):
                    options[raw_key] = raw_value
                    i += 1
                    continue

            if key is not None:
                arg_specs = aliases.get(key) or aliases.get(key.lstrip("-")) or []
                if arg_specs:
                    dest, value_type = arg_specs[0]
                    options[dest] = self._coerce_option_value(value or "", value_type)
                    i += 1
                    continue

            arg_specs = aliases.get(token)
            compact_value = None
            if not arg_specs and token.startswith("-"):
                for alias, specs in aliases.items():
                    if (
                        alias.startswith("-")
                        and len(alias) == 2
                        and token.startswith(alias)
                        and len(token) > len(alias)
                    ):
                        arg_specs = specs
                        compact_value = token[len(alias) :]
                        break

            if arg_specs:
                if len(arg_specs) == 1 and arg_specs[0][1].lower() in {"bool", "boolean"}:
                    options[arg_specs[0][0]] = True
                    i += 1
                    continue
                values: list[str] = []
                if compact_value is not None:
                    values.append(compact_value)
                else:
                    for offset in range(len(arg_specs)):
                        pos = i + 1 + offset
                        if pos < len(tokens):
                            values.append(tokens[pos])
                if len(values) >= len(arg_specs):
                    for (dest, value_type), raw_value in zip(arg_specs, values):
                        options[dest] = self._coerce_option_value(raw_value, value_type)
                    i += 1 + (0 if compact_value is not None else len(arg_specs))
                    continue

            text_tokens.append(token)
            i += 1

        if params.max_texts <= 0:
            texts: list[str] = []
        elif params.max_texts == 1:
            text = " ".join(text_tokens).strip()
            texts = [text] if text else []
        else:
            joined = " ".join(text_tokens)
            if "|" in joined:
                texts = [part.strip() for part in joined.split("|") if part.strip()]
            else:
                texts = [token for token in text_tokens if token]

        return texts, options

    async def collect_params(
        self, event: AstrMessageEvent, params, keyword: str | None = None
    ):
        """收集参数，返回 (images, texts, options)"""
        images: list[tuple[str, bytes]] = []
        texts: list[str] = []
        options: dict[str, bool | str | int | float] = {}
        seen_at_ids: set[str] = set()
        has_explicit_target = False

        chain = event.get_messages()
        send_id: str = event.get_sender_id()
        self_id: str = event.get_self_id()
        sender_name: str = str(event.get_sender_name())
        command_plain_consumed = False

        def _strip_command_text(text: str) -> str:
            nonlocal command_plain_consumed
            stripped = text.strip()
            if command_plain_consumed:
                return stripped
            command_plain_consumed = True

            extra_prefix = str(self.conf.get("extra_prefix") or "").strip()
            for prefix in (extra_prefix, "#"):
                if prefix and stripped.startswith(prefix):
                    stripped = stripped[len(prefix) :].strip()
                    break

            if keyword and stripped.startswith(keyword):
                return stripped[len(keyword) :].strip()

            parts = stripped.split(maxsplit=1)
            return parts[1].strip() if len(parts) > 1 else ""

        async def _append_plain_at_ids(text: str):
            for m in re.finditer(r"[@＠]\s*(\d{1,13})", text):
                target_id = m.group(1)
                if target_id in seen_at_ids:
                    continue
                seen_at_ids.add(target_id)
                nonlocal has_explicit_target
                has_explicit_target = True
                await self._append_id_info(
                    event,
                    target_id,
                    images,
                    options,
                    prefer_front=True,
                )

        async def _process_segment(seg, name: str, collect_text: bool = True):
            if isinstance(seg, Image):
                if src := seg.url or seg.file:
                    if image := await self._decode_image(src):
                        images.append((name, image))
            elif isinstance(seg, At) and seg != chain[0]:
                target_id = str(seg.qq)
                if target_id not in seen_at_ids:
                    seen_at_ids.add(target_id)
                    has_explicit_target = True
                    await self._append_id_info(
                        event,
                        target_id,
                        images,
                        options,
                        prefer_front=True,
                    )
            elif isinstance(seg, Plain):
                plain_text = seg.text.strip()
                await _append_plain_at_ids(plain_text)
                if not collect_text:
                    return
                plain_text = _strip_command_text(plain_text)
                extracted_texts, extracted_options = self._extract_plain_params(
                    plain_text, params
                )
                texts.extend(extracted_texts)
                options.update(extracted_options)

        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if reply_seg and reply_seg.chain:
            name = str(reply_seg.sender_nickname or reply_seg.sender_id)
            for seg in reply_seg.chain:
                await _process_segment(seg, name, collect_text=False)
        for seg in chain:
            await _process_segment(seg, sender_name)

        # 确保图片数量在min_images到max_images之间(参数足够即可)
        if len(images) < params.min_images:
            if sender_avatar := await self.get_avatar(send_id):
                if has_explicit_target:
                    images.append((sender_name, sender_avatar))
                else:
                    images.insert(0, (sender_name, sender_avatar))
        if len(images) < params.min_images:
            if bot_avatar := await self.get_avatar(self_id):
                if has_explicit_target:
                    images.append(("bot", bot_avatar))
                else:
                    images.insert(0, ("bot", bot_avatar))
        images = images[: params.max_images]

        # 确保文本数量在min_texts到max_texts之间(参数足够即可)
        if len(texts) < params.min_texts and params.default_texts:
            texts.extend(params.default_texts)
        texts = texts[: params.max_texts]

        return images, texts, options

    async def close(self):
        if hasattr(self, "session"):
            await self.session.close()
