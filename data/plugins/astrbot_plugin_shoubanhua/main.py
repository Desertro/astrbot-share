import asyncio
import base64
import binascii
import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
import aiohttp
from PIL import Image as PILImage
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.log import LogManager
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from .count_manager import CountManager
from .prompts import get_command_help, get_help_text, prompt_map
logger = LogManager.GetLogger('astrbot_plugin_shoubanhua')
DISABLED_BACKEND_MARKERS = ('api.bltcy.ai', 'bltcy.ai', '柏拉图')
DEFAULT_GPT_MODEL = 'gpt-image-2.5-flare'
_HISTORY_IMAGE_REFERENCE_RE = re.compile('(?i)(?:图(?:片)?\\s*[0-9一二三四五六七八九十]+|第\\s*[0-9一二三四五六七八九十]+\\s*张|上一张|前一张|刚才(?:那|这)?张?图?|最近(?:那|这)?张?图?|这[张幅]图?|那[张幅]图?|参考图|原图)')
_NUMBERED_IMAGE_REFERENCE_RE = re.compile('(?:图(?:片)?|第)\\s*([0-9一二三四五六七八九十]+)\\s*(?:张|幅|个)?')
_AVATAR_TARGET_RE = re.compile('(?:替换[成为]|换[成为]|变[成为])\\s*$')
_CN_NUMERALS = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
EXCLUDED_COMMANDS = ['bna', 'bnn', 'gpt', '手办化添加key', '手办化key列表', '手办化删除key', '手办化增加用户次数', '手办化增加群组次数', '手办化查询次数', '手办化签到']
TRIGGER_WORDS = sorted([key for key in prompt_map.keys() if key not in EXCLUDED_COMMANDS], key=len, reverse=True)
TRIGGER_PATTERN = '|'.join([re.escape(trigger) for trigger in TRIGGER_WORDS])
TRIGGER_PATTERN_WITHOUT_HASH = '|'.join([re.escape(trigger) for trigger in TRIGGER_WORDS])
logger.debug(f'[手办化插件] 构建的正则表达式模式: {TRIGGER_PATTERN}')

@register('astrbot_plugin_shoubanhua', 'shskjw', '通过第三方api进行手办化等功能', '1.2.0', 'https://github.com/shkjw/astrbot_plugin_shoubanhua')
class FigurineProPlugin(Star):

    @filter.after_message_sent()
    async def report_companion_output(self, event: AstrMessageEvent):
        """Publish generation results using actual respond-stage delivery receipts.

        Args:
            event: Original request carrying this plugin's pending result metadata.
        """
        activity = event.get_extra('_mem_chat_shoubanhua_activity', None)
        if not activity:
            return
        event.set_extra('_mem_chat_shoubanhua_activity', None)
        try:
            delivered = event.get_extra('_respond_delivered_components', [])
            status = activity['status']
            if status == 'sent' and (not any((isinstance(part, Image) for part in delivered))):
                status = 'send_failed'
            meta = self.context.get_registered_star('mem_chat')
            target = getattr(meta, 'star_cls', None) if meta and getattr(meta, 'activated', True) else None
            if target and hasattr(target, 'record_plugin_activity'):
                await target.record_plugin_activity(event, plugin='shoubanhua', status=status, summary=activity['summary'] if status != 'send_failed' else '图片已生成，但图片消息发送失败。', chain=delivered if status == 'sent' else None, reference_images=activity.get('reference_images'))
        except Exception as exc:
            logger.warning('[shoubanhua] Failed to publish plugin activity: %s', type(exc).__name__)

    class ImageWorkflow:

        def __init__(self):
            try:
                import aiohttp as _aiohttp
                self.session = _aiohttp.ClientSession(trust_env=True)
            except Exception:
                self.session = None

        async def _download_image(self, url: str) -> bytes | None:
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    if self.session is not None:
                        try:
                            async with self.session.get(url, timeout=30) as resp:
                                resp.raise_for_status()
                                raw = await resp.read()
                                if not raw:
                                    raise ValueError('The image server returned empty content')
                                return raw
                        except aiohttp.ClientConnectionError:
                            if not self.session.trust_env:
                                raise
                            logger.warning('Image connection failed with environment proxy settings; retrying directly')
                            async with aiohttp.ClientSession(trust_env=False) as direct_session:
                                async with direct_session.get(url, timeout=30) as resp:
                                    resp.raise_for_status()
                                    raw = await resp.read()
                                    if not raw:
                                        raise ValueError('The image server returned empty content')
                                    return raw
                    else:
                        import urllib.request

                        def _fetch() -> bytes | None:
                            req = urllib.request.Request(url, method='GET')
                            with urllib.request.urlopen(req, timeout=30) as resp:
                                return resp.read()
                        raw = await asyncio.to_thread(_fetch)
                        if not raw:
                            raise ValueError('服务器返回空内容')
                        return raw
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        delay = 0.5 * (attempt + 1)
                        logger.warning(f'图片下载失败，{delay:.1f} 秒后重试 ({attempt + 1}/3): {e}')
                        await asyncio.sleep(delay)
            logger.error(f'图片下载失败（已重试 3 次）: {last_error}')
            return None

        async def _get_avatar(self, user_id: str) -> bytes | None:
            if not user_id.isdigit():
                logger.warning(f'无法获取非 QQ 平台或无效 QQ 号 {user_id} 的头像。')
                return None
            avatar_url = f'https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640'
            return await self._download_image(avatar_url)

        def _image_source_kind(self, event: AstrMessageEvent) -> tuple[bool, list[str], bool]:
            has_real_image = False
            at_user_ids: list[str] = []
            has_reply_image = False
            try:
                self_id = str(event.get_self_id() or '')
            except Exception:
                self_id = ''
            try:
                for seg in event.message_obj.message:
                    if isinstance(seg, Reply) and seg.chain:
                        for s_chain in seg.chain:
                            if isinstance(s_chain, Image):
                                has_reply_image = True
                                has_real_image = True
                                break
                    elif isinstance(seg, Image):
                        has_real_image = True
                    elif isinstance(seg, At):
                        uid = str(seg.qq)
                        if uid and uid != self_id:
                            at_user_ids.append(uid)
            except Exception:
                pass
            return (has_real_image, at_user_ids, has_reply_image)

        def _extract_first_frame_sync(self, raw: bytes) -> bytes:
            img_io = io.BytesIO(raw)
            try:
                with PILImage.open(img_io) as img:
                    if getattr(img, 'is_animated', False):
                        logger.info('检测到动图, 将抽取第一帧进行生成')
                        img.seek(0)
                        first_frame = img.convert('RGBA')
                        out_io = io.BytesIO()
                        first_frame.save(out_io, format='PNG')
                        return out_io.getvalue()
            except Exception as e:
                logger.warning(f'抽取图片帧时发生错误, 将返回原始数据: {e}', exc_info=True)
            return raw

        async def _load_bytes(self, src: str) -> bytes | None:
            raw: bytes | None = None
            loop = asyncio.get_running_loop()
            if Path(src).is_file():
                raw = await loop.run_in_executor(None, Path(src).read_bytes)
            elif src.startswith('http'):
                raw = await self._download_image(src)
            elif src.startswith('base64://'):
                raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
            if not raw:
                return None
            return await loop.run_in_executor(None, self._extract_first_frame_sync, raw)

        async def get_images(self, event: AstrMessageEvent, max_count: int=4, fallback_avatar_user_id: str | None=None, explicit_image_urls: list[str] | None=None, allow_sender_avatar_fallback: bool=True, reference_text: str='') -> list[bytes]:
            """获取参考图；优先当前消息/引用图，其次按文字引用查历史图。"""
            images = []
            if explicit_image_urls:
                for src in explicit_image_urls:
                    if len(images) >= max_count:
                        break
                    img = await self._load_bytes(str(src or '').strip())
                    if img:
                        images.append(img)
                if images:
                    return images
            for seg in event.message_obj.message:
                if isinstance(seg, Reply) and seg.chain:
                    for s_chain in seg.chain:
                        if isinstance(s_chain, Image):
                            if s_chain.url and (img := (await self._load_bytes(s_chain.url))):
                                images.append(img)
                                if len(images) >= max_count:
                                    return images
                            elif s_chain.file and (img := (await self._load_bytes(s_chain.file))):
                                images.append(img)
                                if len(images) >= max_count:
                                    return images
            at_user_ids: list[str] = []
            target_user_ids: list[str] = []
            plain_parts: list[str] = []
            try:
                self_id = str(event.get_self_id() or '')
            except Exception:
                self_id = ''
            for seg in event.message_obj.message:
                if isinstance(seg, Image) and len(images) < max_count:
                    if seg.url and (img := (await self._load_bytes(seg.url))):
                        images.append(img)
                    elif seg.file and (img := (await self._load_bytes(seg.file))):
                        images.append(img)
                elif isinstance(seg, Plain):
                    plain_parts.append(seg.text)
                elif isinstance(seg, At):
                    uid = str(seg.qq)
                    if uid and uid != self_id:
                        at_user_ids.append(uid)
                        if _AVATAR_TARGET_RE.search(''.join(plain_parts)):
                            target_user_ids.append(uid)
            if target_user_ids:
                at_user_ids = target_user_ids
            reference_numbers = self._numbered_references(reference_text)
            reference_numbers.extend((number for raw in re.findall('([0-9一二三四五六七八九十]+)\\s*图', reference_text) if (number := self._reference_number(raw)) is not None))
            requested_count = max(reference_numbers, default=0)
            explicit_avatar_request = bool(re.search('头像|頭像|avatar', reference_text, re.IGNORECASE))
            needs_avatars = not images or requested_count > len(images) or explicit_avatar_request or bool(target_user_ids)
            if needs_avatars and len(images) < max_count and at_user_ids:
                avatar_limit = max_count
                if images and requested_count and (not explicit_avatar_request):
                    avatar_limit = min(max_count, requested_count)
                seen = set()
                filtered_ids = []
                for uid in at_user_ids:
                    if uid.isdigit() and uid not in seen:
                        seen.add(uid)
                        filtered_ids.append(uid)
                for uid in filtered_ids:
                    if len(images) >= avatar_limit:
                        break
                    avatar = await self._get_avatar(uid)
                    if avatar:
                        images.append(avatar)
            if not images and reference_text and self._has_history_image_reference(reference_text):
                for src in self._history_image_urls(event, reference_text, max_count=max_count):
                    if len(images) >= max_count:
                        break
                    if (img := (await self._load_bytes(src))):
                        images.append(img)
            if not images:
                avatar_uid = ''
                if fallback_avatar_user_id:
                    avatar_uid = str(fallback_avatar_user_id).strip()
                elif allow_sender_avatar_fallback:
                    avatar_uid = str(event.get_sender_id() or '').strip()
                if avatar_uid:
                    if (avatar := (await self._get_avatar(avatar_uid))):
                        images.append(avatar)
            return images

        @staticmethod
        def _has_history_image_reference(text: str) -> bool:
            return bool(_HISTORY_IMAGE_REFERENCE_RE.search(str(text or '')))

        @staticmethod
        def _reference_number(raw: str) -> int | None:
            value = str(raw or '').strip()
            if value.isdigit():
                return int(value)
            return _CN_NUMERALS.get(value)

        @classmethod
        def _numbered_references(cls, text: str) -> list[int]:
            numbers = {number for match in _NUMBERED_IMAGE_REFERENCE_RE.finditer(str(text or '')) if (number := cls._reference_number(match.group(1)))}
            return sorted(numbers)

        @staticmethod
        def _recent_image_burst(infos: list[dict[str, Any]], max_gap_seconds: int=600) -> list[dict[str, Any]]:
            if not infos:
                return []
            burst = [infos[-1]]
            for info in reversed(infos[:-1]):
                newer = burst[0]
                try:
                    gap = (datetime.fromisoformat(str(newer.get('timestamp') or '')) - datetime.fromisoformat(str(info.get('timestamp') or ''))).total_seconds()
                except Exception:
                    gap = 0
                if gap < 0 or gap > max_gap_seconds:
                    break
                burst.insert(0, info)
            return burst

        @classmethod
        def _select_history_image_infos(cls, infos: list[dict[str, Any]], reference_text: str, sender_id: str='') -> list[dict[str, Any]]:
            available = [item for item in infos if str(item.get('url') or '').strip()]
            if not available:
                return []
            sender = str(sender_id or '')
            sender_items = [item for item in available if sender and str(item.get('sender_id') or '') == sender]
            candidates = sender_items or available
            numbers = cls._numbered_references(reference_text)
            if numbers:
                needed = max(numbers)
                burst = cls._recent_image_burst(candidates)
                return burst[:needed] if len(burst) >= needed else candidates[-needed:]
            text = str(reference_text or '')
            if re.search('上一张|前一张', text) and len(candidates) >= 2:
                return [candidates[-2]]
            return [candidates[-1]]

        def _history_image_urls(self, event: AstrMessageEvent, reference_text: str, max_count: int=4) -> list[str]:
            try:
                from data.plugins.astrbot_plugin_imgexploration.core.image_context import get_image_context_manager
                info = get_image_context_manager().get_image_context_info(event)
            except Exception as exc:
                logger.debug(f'[手办化插件] 读取历史图片缓存失败: {exc}')
                return []
            selected = self._select_history_image_infos(list(info.get('images') or []), reference_text, sender_id=str(event.get_sender_id() or ''))
            urls = [str(item.get('url') or '') for item in selected if item.get('url')]
            if urls:
                logger.info('[手办化插件] 从会话图片缓存解析参考图: refs=%s images=%s', self._numbered_references(reference_text), len(urls))
            return urls[:max_count]

        async def terminate(self):
            if self.session and (not getattr(self.session, 'closed', True)):
                await self.session.close()

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.plugin_data_dir = Path(StarTools.get_data_dir()) / 'plugin_data' / 'astrbot_plugin_shoubanhua'
        self.plugin_data_dir.mkdir(parents=True, exist_ok=True)
        self.count_manager = CountManager(self.plugin_data_dir, lambda k, d=None: self.conf.get(k, d), self.conf.set)
        self.key_index = 0
        self.key_lock = asyncio.Lock()
        self.iwf: FigurineProPlugin.ImageWorkflow | None = None

    async def initialize(self):
        logger.info('已从prompts.py加载提示词配置')
        prefix = self.conf.get('prefix', True)
        self.set_prefix_config(prefix)
        self.iwf = self.ImageWorkflow()
        await self.count_manager.initialize()

    def is_global_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为机器人的全局管理员"""
        admin_ids = self.context.get_config().get('admins_id', [])
        return event.get_sender_id() in admin_ids

    def _strip_command_prefix(self, text: str, command: str) -> str:
        if not text:
            return ''
        pattern = f'^\\s*#?\\s*{re.escape(command)}\\b'
        return re.sub(pattern, '', text, count=1, flags=re.IGNORECASE).strip()

    def _extract_user_prompt(self, event: AstrMessageEvent, command: str) -> str:
        """Extract text while preserving inline avatar replacement targets.

        Args:
            event: Incoming command and its message components.
            command: Command prefix to remove.

        Returns:
            Prompt text with explicit replacement mentions described as avatars.
        """
        chain = getattr(getattr(event, 'message_obj', None), 'message', None)
        if chain:
            parts: list[str] = []
            for seg in chain:
                if isinstance(seg, Plain):
                    parts.append(seg.text)
                elif isinstance(seg, At) and str(seg.qq).isdigit() and (str(seg.qq) != str(event.get_self_id() or '')) and _AVATAR_TARGET_RE.search(''.join(parts)):
                    parts.append('被@用户的头像（见附加的头像参考图）')
            plain_text = ''.join(parts).strip()
            parsed = self._strip_command_prefix(plain_text, command)
            if parsed:
                return parsed
        return self._strip_command_prefix((event.message_str or '').strip(), command)

    @filter.command('手办化签到', prefix_optional=True)
    async def on_checkin(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        success, message = await self.count_manager.checkin(user_id)
        yield event.plain_result(message)

    @filter.command('手办化增加用户次数', prefix_optional=True)
    async def on_add_user_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            logger.debug(f'用户 {event.get_sender_id()} 尝试使用管理员命令但权限不足')
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        admin_id = event.get_sender_id()
        success, message = await self.count_manager.add_user_counts(event)
        logger.info(f'管理员 {admin_id} {message}')
        yield event.plain_result(message)

    @filter.command('手办化增加群组次数', prefix_optional=True)
    async def on_add_group_counts(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        admin_id = event.get_sender_id()
        success, message = await self.count_manager.add_group_counts(event)
        logger.info(f'管理员 {admin_id} {message}')
        yield event.plain_result(message)

    @filter.command('手办化查询次数', prefix_optional=True)
    async def on_query_counts(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_admin = self.is_global_admin(event)
        success, message = await self.count_manager.query_counts(user_id, group_id, is_admin, event)
        yield event.plain_result(message)
        return

    @filter.command('手办化添加key', prefix_optional=True)
    async def on_add_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        key = event.message_str.replace('手办化添加key', '').strip()
        if not key:
            yield event.plain_result('❌ 请提供 API Key。用法: 手办化添加key sk-...')
            return
        keys = self.conf.get('api_keys', [])
        if key in keys:
            yield event.plain_result('❌ 该 Key 已存在')
            return
        keys.append(key)
        self.conf['api_keys'] = keys
        self.conf.save_config()
        yield event.plain_result(f'✅ 已添加 Key。当前共有 {len(keys)} 个 Key。')

    @filter.command('手办化key列表', prefix_optional=True)
    async def on_list_keys(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        keys = self.conf.get('api_keys', [])
        if not keys:
            yield event.plain_result('当前未配置任何插件专用 Key，请使用 #手办化添加key 命令进行添加。')
            return
        msg = ['当前配置的 Key 列表:']
        for i, k in enumerate(keys):
            masked = k[:8] + '******' + k[-4:] if len(k) > 12 else '******'
            msg.append(f'{i + 1}. {masked}')
        yield event.plain_result('\n'.join(msg))

    @filter.command('手办化删除key', prefix_optional=True)
    async def on_delete_key(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        target = event.message_str.replace('手办化删除key', '').strip()
        if not target:
            yield event.plain_result('❌ 请指定要删除的 Key (完整字符串) 或 序号。用法: 手办化删除key <key|index>')
            return
        keys = self.conf.get('api_keys', [])
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(keys):
                keys.pop(idx)
                self.conf['api_keys'] = keys
                self.conf.save_config()
                yield event.plain_result(f'✅ 已删除第 {idx + 1} 个 Key')
                return
        if target in keys:
            keys.remove(target)
            self.conf['api_keys'] = keys
            self.conf.save_config()
            yield event.plain_result('✅ 已删除指定 Key')
            return
        yield event.plain_result('❌ 未找到指定的 Key')

    @filter.command('手办化模型查看', prefix_optional=True)
    async def on_model_list(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            logger.debug(f'用户 {event.get_sender_id()} 尝试使用管理员命令但权限不足')
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        remote = await self._list_remote_models()
        models = remote or self.conf.get('image_models', [])
        current = self.conf.get('model_name', 'nano-banana')
        if not models:
            models = [current]
        lines = [f'当前模型: {current}', '可用模型:'] + [f'- {m}' for m in models]
        yield event.plain_result('\n'.join(lines))

    @filter.regex('^手办化模型切换\\s+(.+)')
    async def on_model_switch(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            logger.debug(f'用户 {event.get_sender_id()} 尝试使用管理员命令但权限不足')
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        msg = event.message_str.strip()
        m = re.match('^手办化模型切换\\s+(.+)$', msg)
        if not m:
            return
        target = m.group(1).strip()
        remote = await self._list_remote_models()
        models = remote or self.conf.get('image_models', [])
        if models and target not in models:
            current = self.conf.get('model_name', 'nano-banana')
            lines = [f'当前模型: {current}', f'不可用: {target}', '可用模型:'] + [f'- {m}' for m in models]
            yield event.plain_result('\n'.join(lines))
            return
        self.conf.model_name = target
        try:
            self.conf.save_config()
        except Exception:
            pass
        yield event.plain_result(f'✅ 已切换模型为: {target}')

    @filter.command('手办化使用本地模型', prefix_optional=True)
    async def on_use_local_model(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        self.conf['use_local_zimage'] = True
        try:
            self.conf.save_config()
        except Exception:
            pass
        yield event.plain_result('✅ 已切换为本地 Z-Image 模型')

    @filter.command('手办化使用云端模型', prefix_optional=True)
    async def on_use_remote_model(self, event: AstrMessageEvent):
        if not self.is_global_admin(event):
            yield event.plain_result('❌ 权限不足: 此命令仅限机器人管理员使用')
            return
        self.conf['use_local_zimage'] = False
        try:
            self.conf.save_config()
        except Exception:
            pass
        yield event.plain_result('✅ 已切换回云端模型')

    @filter.command('手办化当前后端', prefix_optional=True)
    async def on_backend_status(self, event: AstrMessageEvent):
        use_local = bool(self.conf.get('use_local_zimage', False))
        if use_local:
            url = self.conf.get('local_zimage_url', '未配置')
            yield event.plain_result(f'当前手办化后端: 本地 Z-Image\nURL: {url}')
            return
        backends = self._build_remote_backends()
        if not backends:
            yield event.plain_result('当前未配置任何云端后端')
            return
        lines = ['当前手办化后端配置:']
        for backend in backends:
            keys = self._normalize_api_keys(backend.get('api_keys', []))
            lines.append(f'- {backend['name']}: {backend['api_format']} | bnn模型: {backend['model_name']} | gpt模型: {' -> '.join(backend.get('gpt_model_names') or [backend.get('gpt_model_name', backend['model_name'])])} | URL: {backend['api_url']} | Keys: {len(keys)}')
        yield event.plain_result('\n'.join(lines))

    def _normalize_api_keys(self, raw_keys: Any) -> list[str]:
        if not isinstance(raw_keys, list):
            return []
        keys: list[str] = []
        for key in raw_keys:
            if not isinstance(key, str):
                continue
            normalized = key.strip()
            if normalized:
                keys.append(normalized)
        return keys

    def _normalize_resolution(self, resolution: str | None, default: str='500p') -> str:
        res_norm = str(resolution or default).strip().lower()
        alias_map = {'0.5k': '500p', '500': '500p', '500p': '500p', '512': '500p', '512px': '500p', '1k': '1k', '1024': '1k', '1024px': '1k', '2k': '2k', '2048': '2k', '2048px': '2k', '4k': '4k', '4096': '4k', '4096px': '4k'}
        return alias_map.get(res_norm, default)

    def _extract_image_dimensions(self, raw: bytes) -> tuple[int, int] | None:
        try:
            with PILImage.open(io.BytesIO(raw)) as img:
                width, height = img.size
                if width > 0 and height > 0:
                    return (int(width), int(height))
        except Exception as e:
            logger.debug(f'[手办化插件] 读取参考图尺寸失败: {e}')
        return None

    @staticmethod
    def _infer_aspect_reference_index(prompt: str, image_count: int) -> int | None:
        if image_count <= 0:
            return None
        text = str(prompt or '')
        subject_words = re.compile('人物|角色|姿势|动作|构图|比例|主体|服装|脸|画幅|横|竖|尺寸')
        matches = list(_NUMBERED_IMAGE_REFERENCE_RE.finditer(text))
        for position, match in enumerate(matches):
            raw = match.group(1)
            number = int(raw) if raw.isdigit() else _CN_NUMERALS.get(raw)
            if not number or number > image_count:
                continue
            segment_end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
            segment = text[match.end():min(segment_end, match.end() + 32)]
            if subject_words.search(segment):
                return number - 1
        return None

    def _infer_requested_aspect_ratio(self, image_bytes_list: list[bytes], prompt: str, preferred_index: int | None=None) -> tuple[float, str]:
        ordered = list(image_bytes_list)
        if preferred_index is not None and 0 <= preferred_index < len(ordered):
            ordered.insert(0, ordered.pop(preferred_index))
        for raw in ordered:
            dims = self._extract_image_dimensions(raw)
            if dims:
                width, height = dims
                ratio = float(width) / float(height)
                return (ratio, f'reference:{width}x{height}')
        text = str(prompt or '').lower()
        portrait_patterns = ['9\\s*[:：/xX]\\s*16', '3\\s*[:：/xX]\\s*4', '竖图', '竖版', '长图', '手机壁纸', 'portrait', 'vertical', 'tall', 'poster', '海报']
        landscape_patterns = ['16\\s*[:：/xX]\\s*9', '4\\s*[:：/xX]\\s*3', '3\\s*[:：/xX]\\s*2', '横图', '横版', '宽图', '电脑壁纸', 'banner', 'landscape', 'wide', 'cover', '封面']
        square_patterns = ['1\\s*[:：/xX]\\s*1', '方图', '正方形', 'square', 'icon', '头像']
        if any((re.search(p, text, re.IGNORECASE) for p in portrait_patterns)):
            return (9.0 / 16.0, 'prompt:portrait')
        if any((re.search(p, text, re.IGNORECASE) for p in landscape_patterns)):
            return (16.0 / 9.0, 'prompt:landscape')
        if any((re.search(p, text, re.IGNORECASE) for p in square_patterns)):
            return (1.0, 'prompt:square')
        return (1.0, 'default:square')

    def _fit_dimension_to_multiple(self, value: float, minimum: int=256, multiple: int=64) -> int:
        rounded = int(round(float(value) / float(multiple)) * multiple)
        return max(minimum, rounded)

    def _closest_aspect_ratio_label(self, ratio: float) -> str:
        candidates = {'1:1': 1.0, '4:3': 4.0 / 3.0, '3:4': 3.0 / 4.0, '3:2': 3.0 / 2.0, '2:3': 2.0 / 3.0, '16:9': 16.0 / 9.0, '9:16': 9.0 / 16.0}
        return min(candidates.items(), key=lambda item: abs(item[1] - ratio))[0]

    def _choose_canvas_size(self, resolution: str | None, image_bytes_list: list[bytes], prompt: str) -> tuple[str, str, str, str]:
        res_norm = self._normalize_resolution(resolution, default='1k')
        preferred_index = self._infer_aspect_reference_index(prompt, len(image_bytes_list))
        ratio, reason = self._infer_requested_aspect_ratio(image_bytes_list, prompt, preferred_index=preferred_index)
        short_edge_map = {'500p': 512, '1k': 1024, '2k': 2048, '4k': 2304}
        long_edge_cap_map = {'500p': 896, '1k': 1792, '2k': 3584, '4k': 4096}
        image_size_param_map = {'500p': '512px', '1k': '1K', '2k': '2K', '4k': '4K'}
        short_edge = short_edge_map[res_norm]
        long_edge_cap = long_edge_cap_map[res_norm]
        image_size_param = image_size_param_map[res_norm]
        if 0.85 <= ratio <= 1.18:
            width = short_edge
            height = short_edge
        elif ratio > 1.0:
            width = min(long_edge_cap, self._fit_dimension_to_multiple(short_edge * ratio))
            height = self._fit_dimension_to_multiple(width / ratio)
            if height > short_edge:
                height = short_edge
                width = min(long_edge_cap, self._fit_dimension_to_multiple(height * ratio))
        else:
            inv_ratio = 1.0 / max(ratio, 0.01)
            height = min(long_edge_cap, self._fit_dimension_to_multiple(short_edge * inv_ratio))
            width = self._fit_dimension_to_multiple(height * ratio)
            if width > short_edge:
                width = short_edge
                height = min(long_edge_cap, self._fit_dimension_to_multiple(width * inv_ratio))
        width = min(long_edge_cap, max(256, width))
        height = min(long_edge_cap, max(256, height))
        size_val = f'{width}x{height}'
        aspect_ratio = self._closest_aspect_ratio_label(float(width) / float(height))
        logger.info(f'[手办化插件] 自动推断画布: resolution={res_norm} size={size_val} aspect_ratio={aspect_ratio} image_size={image_size_param} reason={reason} ratio={ratio:.3f}')
        return (size_val, image_size_param, aspect_ratio, reason)

    def _build_gpt_image_prompt(self, prompt: str, image_count: int=0) -> str:
        base_prompt = str(prompt or '').strip()
        lines = ['Generate a complete image directly.', 'Return only the final image result without any explanation text.', "Follow the user's request faithfully and keep the subject clear and visually coherent."]
        if image_count > 0:
            lines.append(f'Use the provided {image_count} reference image(s) as visual reference while preserving the requested style and composition intent.')
        if base_prompt:
            lines.append(f'User request: {base_prompt}')
        else:
            lines.append('User request: Generate a detailed, visually clear image based on the provided command intent.')
        return '\n'.join(lines)

    def _gpt_image_quality(self) -> str:
        """读取 GPT 图像质量；新版 gpt-image 接口使用 low/medium/high/auto。"""
        quality = str(self.conf.get('gpt_image_quality', 'auto')).strip().lower()
        if quality not in {'low', 'medium', 'high', 'auto'}:
            logger.warning(f'[手办化插件] gpt_image_quality={quality!r} 无效，回退到 auto')
            return 'auto'
        return quality

    @staticmethod
    def _build_gpt_image_generation_payload(model_name: str, prompt: str, size: str='auto', quality: str='auto') -> dict[str, Any]:
        """构造 GPT 生图请求；size/quality=auto 时由模型自行决定。"""
        return {'model': model_name, 'prompt': prompt, 'size': size, 'quality': quality, 'n': 1}

    def _resolve_command_family(self, trigger_word: str) -> str:
        if trigger_word in {'bnn', 'bna'}:
            return 'bnn'
        return 'gpt'

    def _is_disabled_backend_url(self, *values: Any) -> bool:
        combined = ' '.join((str(value or '').strip().lower() for value in values))
        return any((marker.lower() in combined for marker in DISABLED_BACKEND_MARKERS))

    def _derive_openai_base_url(self, api_url: str | None, configured_base: str | None=None) -> str:
        if configured_base:
            return str(configured_base).rstrip('/')
        if api_url and '/v1' in str(api_url):
            return str(api_url).split('/v1')[0].rstrip('/')
        return ''

    def _derive_google_base_url(self, api_url: str | None, configured_base: str | None=None) -> str:
        if configured_base:
            return str(configured_base).rstrip('/')
        if api_url and '/v1' in str(api_url):
            return str(api_url).split('/v1')[0].rstrip('/')
        return __import__('share_runtime').resolve('{{AB_FIGURINE_WORKSHOP_API_BASE_URL}}')

    def _normalize_model_names(self, raw_names: Any) -> list[str]:
        if isinstance(raw_names, str):
            raw_names = re.split('[,\\n]', raw_names)
        if not isinstance(raw_names, (list, tuple)):
            return []
        names: list[str] = []
        seen: set[str] = set()
        for raw_name in raw_names:
            name = str(raw_name or '').strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

    def _build_remote_backends(self) -> list[dict[str, Any]]:
        backends: list[dict[str, Any]] = []
        primary_keys = self._normalize_api_keys(self.conf.get('api_keys', []))
        primary_api_url = str(self.conf.get('api_url', '') or '').strip()
        primary_model = str(self.conf.get('model_name', 'nano-banana') or 'nano-banana').strip()
        primary_gpt_model = str(self.conf.get('gpt_model_name', DEFAULT_GPT_MODEL) or DEFAULT_GPT_MODEL).strip()
        primary_gpt_models = self._normalize_model_names(self.conf.get('gpt_model_names', [])) or [primary_gpt_model]
        primary_format = str(self.conf.get('api_format', 'openai') or 'openai').strip().lower()
        primary_google_base = str(self.conf.get('google_api_base', '') or '').strip()
        if primary_keys and primary_api_url:
            primary_base = self.conf.get('blt_api_base')
            if self._is_disabled_backend_url(primary_api_url, primary_google_base, primary_base):
                logger.warning('[手办化插件] 主后端指向已禁用的 BLT/柏拉图地址，已跳过')
            else:
                backends.append({'name': '主后端', 'api_url': primary_api_url, 'api_format': primary_format, 'google_api_base': primary_google_base, 'blt_api_base': primary_base, 'model_name': primary_model, 'gpt_model_name': primary_gpt_model, 'gpt_model_names': primary_gpt_models, 'api_keys': primary_keys})
        backup_keys = self._normalize_api_keys(self.conf.get('backup_api_keys', []))
        backup_api_url = str(self.conf.get('backup_api_url', '') or '').strip()
        backup_model = str(self.conf.get('backup_model_name', '') or '').strip()
        backup_gpt_model = str(self.conf.get('backup_gpt_model_name', DEFAULT_GPT_MODEL) or DEFAULT_GPT_MODEL).strip()
        backup_format = str(self.conf.get('backup_api_format', 'openai') or 'openai').strip().lower()
        backup_google_base = str(self.conf.get('backup_google_api_base', '') or '').strip()
        if self.conf.get('backup_enabled', True) and backup_keys and backup_api_url and (backup_model or backup_gpt_model):
            backup_base = self.conf.get('backup_blt_api_base')
            if self._is_disabled_backend_url(backup_api_url, backup_google_base, backup_base):
                logger.warning('[手办化插件] 备后端指向已禁用的 BLT/柏拉图地址，已跳过')
            else:
                backends.append({'name': '备后端', 'api_url': backup_api_url, 'api_format': backup_format, 'google_api_base': backup_google_base, 'blt_api_base': backup_base, 'model_name': backup_model, 'gpt_model_name': backup_gpt_model, 'bnn_enabled': self.conf.get('backup_bnn_enabled', True), 'api_keys': backup_keys})
        third_keys = self._normalize_api_keys(self.conf.get('third_api_keys', []))
        third_api_url = str(self.conf.get('third_api_url', '') or '').strip()
        third_model = str(self.conf.get('third_model_name', '') or '').strip()
        third_gpt_model = str(self.conf.get('third_gpt_model_name', DEFAULT_GPT_MODEL) or DEFAULT_GPT_MODEL).strip()
        third_format = str(self.conf.get('third_api_format', 'openai') or 'openai').strip().lower()
        third_google_base = str(self.conf.get('third_google_api_base', '') or '').strip()
        if third_keys and third_api_url and third_model:
            third_base = self.conf.get('third_blt_api_base')
            if self._is_disabled_backend_url(third_api_url, third_google_base, third_base):
                logger.warning('[手办化插件] 第三后端指向已禁用的 BLT/柏拉图地址，已跳过')
            else:
                backends.append({'name': '第三后端', 'api_url': third_api_url, 'api_format': third_format, 'google_api_base': third_google_base, 'blt_api_base': third_base, 'model_name': third_model, 'gpt_model_name': third_gpt_model, 'api_keys': third_keys})
        return backends

    def _resolve_runtime_model_name(self, backend: dict[str, Any], model_name: str, resolution: str | None) -> str:
        model = str(model_name or '').strip()
        if not model:
            return model
        api_format = str(backend.get('api_format', 'openai') or 'openai').strip().lower()
        if api_format == 'google':
            if model.startswith('gemini-3.1-flash-image-preview-'):
                logger.info(f'[手办化插件] Google 图片模型统一收敛为基础模型: {model} -> gemini-3.1-flash-image-preview')
                return 'gemini-3.1-flash-image-preview'
            return model
        if api_format != 'openai':
            return model
        base_url = self._derive_openai_base_url(backend.get('api_url'), backend.get('blt_api_base'))
        if resolution is None:
            return model
        if not model.startswith('gemini-3.1-flash-image'):
            return model
        res_norm = self._normalize_resolution(resolution)
        if base_url.rstrip('/') == __import__('share_runtime').resolve('{{AB_MEM_CHAT_MEME_VISION_FALLBACK_BASE_URL}}'):
            if res_norm not in {'default', '1k'}:
                logger.info(f'[手办化插件] moyuu 无 {res_norm} 专用模型，回退到可用基础模型: {model}')
            return model
        if '91vip.txvlogvip.top:3000' in base_url:
            resolved_map = {'500p': 'gemini-3.1-flash-image-0.5k', '1k': 'gemini-3.1-flash-image-1k-flow', '2k': 'gemini-3.1-flash-image-2k-flow', '4k': 'gemini-3.1-flash-image-4k-flow'}
            resolved = resolved_map[res_norm]
            if resolved != model:
                logger.info(f'[手办化插件] 91vip 图片模型按分辨率映射: {model} -> {resolved} (resolution={res_norm})')
            return resolved
        return model

    def _get_backend_model_candidates(self, backend: dict[str, Any], command_family: str, resolution: str | None) -> list[str]:
        if command_family == 'gpt':
            raw_models = self._normalize_model_names(backend.get('gpt_model_names'))
            if not raw_models:
                raw_models = [str(backend.get('gpt_model_name') or backend.get('model_name') or DEFAULT_GPT_MODEL).strip()]
        else:
            raw_models = [str(backend.get('model_name', 'nano-banana') or 'nano-banana').strip()]
        models: list[str] = []
        seen: set[str] = set()
        for raw_model in raw_models:
            model = self._resolve_runtime_model_name(backend, raw_model, resolution)
            if model and model not in seen:
                seen.add(model)
                models.append(model)
        return models

    def _ordered_backend_models(self, backend: dict[str, Any], command_family: str, resolution: str | None) -> list[str]:
        models = self._get_backend_model_candidates(backend, command_family, resolution)
        if command_family != 'gpt' or len(models) <= 1:
            return models
        backend_key = (str(backend.get('name', '')), str(backend.get('api_url', '')))
        cursors = getattr(self, '_gpt_model_cursors', None)
        if cursors is None:
            cursors = {}
            self._gpt_model_cursors = cursors
        start = cursors.get(backend_key, 0) % len(models)
        cursors[backend_key] = (start + 1) % len(models)
        return models[start:] + models[:start]

    def _resolve_backend_model_name(self, backend: dict[str, Any], command_family: str, resolution: str | None) -> str:
        models = self._get_backend_model_candidates(backend, command_family, resolution)
        return models[0] if models else ''

    async def _probe_backend(self, backend: dict[str, Any], api_key: str) -> tuple[bool, str]:
        if not self.iwf:
            return (False, 'ImageWorkflow 未初始化')
        api_format = str(backend.get('api_format', 'openai') or 'openai').strip().lower()
        timeout = int(self.conf.get('backend_probe_timeout', 12) or 12)
        try:
            if api_format == 'google':
                base_url = self._derive_google_base_url(backend.get('api_url'), backend.get('google_api_base'))
                probe_url = f'{base_url}/v1beta/models'
                async with self.iwf.session.get(probe_url, params={'key': api_key}, timeout=timeout) as resp:
                    if resp.status == 200:
                        return (True, 'ok')
                    text = await resp.text()
                    return (False, f'HTTP {resp.status}: {text[:120]}')
            base_url = self._derive_openai_base_url(backend.get('api_url'), backend.get('blt_api_base'))
            if not base_url or self._is_disabled_backend_url(base_url):
                return (False, '后端地址未配置或已禁用')
            probe_url = f'{base_url}/v1/models'
            headers = {'Authorization': f'Bearer {api_key}'}
            async with self.iwf.session.get(probe_url, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    return (True, 'ok')
                text = await resp.text()
                return (False, f'HTTP {resp.status}: {text[:120]}')
        except asyncio.TimeoutError:
            return (False, '探活超时')
        except Exception as e:
            return (False, str(e))

    async def _list_remote_models(self) -> list[str]:
        try:
            backends = self._build_remote_backends()
            if not backends:
                return []
            backend = backends[0]
            keys = self._normalize_api_keys(backend.get('api_keys', []))
            key = keys[0] if keys else None
            base = self._derive_openai_base_url(backend.get('api_url'), backend.get('blt_api_base'))
            if not base or self._is_disabled_backend_url(base):
                return []
            url = f'{base}/v1/models'
            headers = {'Content-Type': 'application/json'}
            if key:
                headers['Authorization'] = f'Bearer {key}'
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession(trust_env=True) as s:
                async with s.get(url, headers=headers, timeout=20) as resp:
                    if resp.status != 200:
                        t = await resp.text()
                        logger.warning(f'获取模型列表失败: HTTP {resp.status} {t[:200]}')
                        return []
                    data = await resp.json()
                    items = []
                    try:
                        for m in data.get('data') or []:
                            mid = m.get('id') or m.get('name') or ''
                            if mid:
                                items.append(mid)
                    except Exception:
                        pass
                    if not items and isinstance(data, dict):
                        for k in ('models', 'available', 'list'):
                            v = data.get(k)
                            if isinstance(v, list):
                                for mid in v:
                                    if isinstance(mid, str):
                                        items.append(mid)
                    if not items:
                        return []
                    filtered = [m for m in items if any((x in m.lower() for x in ['image', 'vision', 'multimodal', 'img', 'photo', 'doubao', 'banana', 'dream', 'art', 'gen', 'flux', 'gpt', 'gemini']))]
                    return filtered or items
        except Exception as e:
            logger.warning(f'获取模型列表异常: {e}')
            return []

    @filter.command('bna', prefix_optional=True)
    async def on_bna(self, event: AstrMessageEvent):
        user_prompt = self._extract_user_prompt(event, 'bna')
        async for result in self._process_figurine_request(event, 'bna', user_prompt):
            yield result

    @filter.regex('(?i)^#?bnn(?:(?:1k|2k|4k)\\b|\\b).*')
    async def on_bnn(self, event: AstrMessageEvent):
        match = re.match('(?is)^#?bnn(?:\\s*(1k|2k|4k)\\b|\\b)\\s*(.*)$', event.message_str.strip())
        if not match:
            return
        resolution = match.group(1).lower() if match.group(1) else None
        user_prompt = match.group(2).strip()
        async for result in self._process_figurine_request(event, 'bnn', user_prompt, resolution=resolution):
            yield result

    @filter.regex('(?i)^#?(?:1k|2k|4k)(?:\\s|$)')
    async def on_resolution_bnn(self, event: AstrMessageEvent):
        match = re.match('(?is)^#?(1k|2k|4k)(?:\\s+(.*)|$)', event.message_str.strip())
        if not match:
            return
        async for result in self._process_figurine_request(event, 'bnn', (match.group(2) or '').strip(), resolution=match.group(1).lower()):
            yield result

    @filter.regex('^#?gpt\\b.*')
    async def on_gpt(self, event: AstrMessageEvent):
        message_text = event.message_str.strip()
        user_prompt = self._extract_user_prompt(event, 'gpt')
        logger.info(f'[手办化插件] on_gpt函数被调用，接收到消息: {message_text}')
        logger.info(f'[手办化插件] 提取的用户提示词: {user_prompt}')
        async for result in self._process_figurine_request(event, 'gpt', user_prompt, resolution='1k'):
            yield result

    async def _process_figurine_request(self, event: AstrMessageEvent, trigger_word: str, user_prompt: str | None=None, resolution: str | None=None, fallback_avatar_user_id: str | None=None, explicit_image_urls: list[str] | None=None):
        free_prompt_triggers = {'bnn', 'bna', 'gpt'}
        if trigger_word not in prompt_map and trigger_word not in free_prompt_triggers:
            logger.warning(f"触发词 '{trigger_word}' 未在prompt_map中找到")
            yield event.plain_result(f"❌ 预设 '{trigger_word}' 未在配置中找到。")
            return
        base_prompt = None
        if trigger_word in free_prompt_triggers:
            base_prompt = user_prompt
        else:
            prompt_entry = prompt_map.get(trigger_word, {})
            if isinstance(prompt_entry, dict) and 'prompt' in prompt_entry:
                base_prompt = prompt_entry['prompt']
            else:
                base_prompt = str(prompt_entry)
        if not base_prompt:
            yield event.plain_result(f"❌ 预设 '{trigger_word}' 的提示词为空。")
            return
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        is_master = self.is_global_admin(event)
        if not is_master:
            user_blacklist = self.conf.get('user_blacklist', [])
            if user_blacklist and sender_id in user_blacklist:
                logger.info(f'FigurinePro: 拒绝黑名单用户 {sender_id} 的请求')
                return
            group_blacklist = [str(g) for g in self.conf.get('group_blacklist', [])]
            logger.debug(f'群聊黑名单配置: {group_blacklist}')
            if group_id and group_blacklist:
                group_id_str = str(group_id)
                if group_id_str in group_blacklist:
                    logger.info(f'FigurinePro: 拒绝黑名单群组 {group_id_str} 的请求')
                    return
            user_whitelist = self.conf.get('user_whitelist', [])
            if user_whitelist and sender_id not in user_whitelist:
                logger.info(f'FigurinePro: 拒绝非白名单用户 {sender_id} 的请求')
                return
            group_whitelist = [str(g) for g in self.conf.get('group_whitelist', [])]
            logger.debug(f'群聊白名单配置: {group_whitelist}')
            logger.debug(f'当前群组ID: {group_id}')
            if group_id and group_whitelist:
                group_id_str = str(group_id)
                if group_id_str not in group_whitelist:
                    logger.info(f'FigurinePro: 拒绝非白名单群组 {group_id_str} 的请求')
                    return
            can_use, error_msg = await self.count_manager.check_and_decrease_quota(sender_id, group_id)
            if not can_use and error_msg:
                yield event.plain_result(error_msg)
                return
        if trigger_word == 'bna':
            img_bytes_list = []
        else:
            img_bytes_list = []
            has_image_intent = False
            if explicit_image_urls:
                has_image_intent = True
            if self.iwf:
                has_real_image, at_user_ids, has_reply_image = self.iwf._image_source_kind(event)
                if has_real_image or has_reply_image or at_user_ids:
                    has_image_intent = True
            if self.iwf and self.iwf._has_history_image_reference(base_prompt):
                has_image_intent = True
            allow_sender_avatar_fallback = trigger_word not in free_prompt_triggers
            if self.iwf:
                img_bytes_list = await self.iwf.get_images(event, fallback_avatar_user_id=fallback_avatar_user_id, explicit_image_urls=explicit_image_urls, allow_sender_avatar_fallback=allow_sender_avatar_fallback, reference_text=base_prompt)
            if trigger_word not in free_prompt_triggers and (not img_bytes_list):
                yield event.plain_result('请发送或引用图片（最多4张），或@一个或多个用户再试。')
                return
            if trigger_word in free_prompt_triggers and has_image_intent and (not img_bytes_list):
                yield event.plain_result('检测到你提到了参考图、引用了图片或@用户，但没能读取到参考图。如果图片在更早的消息里，请重新引用或补发；也可以直接说明是哪一张。')
                return
        final_prompt = base_prompt
        if user_prompt and trigger_word not in free_prompt_triggers:
            final_prompt = f'{base_prompt}\n\n用户补充要求: {user_prompt}'
        if len(img_bytes_list) > 1:
            final_prompt = f'{final_prompt}\n\n参考图按输入顺序编号为图1、图2……（共{len(img_bytes_list)}张）。严格按用户指定的分工使用：若用户说某张只参考画风，就只提取其画风、配色和氛围，禁止复制该图的人物、姿势、手持物、道具和构图；若用户指定另一张决定人物、动作或构图，必须以那张为主体依据，并优先继承它的画幅比例。'
        event.set_extra('_mem_chat_shoubanhua_activity', {'status': 'running', 'summary': f'正在按 {trigger_word} 预设生成图片。', 'reference_images': img_bytes_list})
        yield event.plain_result(f'🎨 收到请求，正在生成 [{trigger_word}] 风格图片...')
        start_time = datetime.now()
        if bool(self.conf.get('use_local_zimage', False)):
            res = await self._call_local_zimage(img_bytes_list, final_prompt, resolution)
            model_info = '本地 Z-Image' if isinstance(res, bytes) else None
        else:
            res = await self._call_api(img_bytes_list, final_prompt, resolution, command_family=self._resolve_command_family(trigger_word))
            model_info = None
            if isinstance(res, tuple):
                res, model_info = res
        elapsed = (datetime.now() - start_time).total_seconds()
        if isinstance(res, bytes):
            if not is_master:
                logger.debug(f'用户 {sender_id} 成功使用了 {trigger_word} 功能')
            caption_parts = [f'✅ 生成成功 ({elapsed:.2f}s)', f'预设: {trigger_word}']
            if model_info:
                caption_parts.append(f'模型: {model_info}')
            if len(img_bytes_list) > 1:
                caption_parts.append(f'使用了{len(img_bytes_list)}张图片')
            if is_master:
                caption_parts.append('剩余次数: ∞')
            else:
                if self.conf.get('enable_user_limit', True):
                    caption_parts.append(f'个人剩余: {self.count_manager._get_user_count(sender_id)}')
                if self.conf.get('enable_group_limit', False) and group_id:
                    caption_parts.append(f'本群剩余: {self.count_manager._get_group_count(group_id)}')
            event.set_extra('_mem_chat_shoubanhua_activity', {'status': 'sent', 'summary': f'按 {trigger_word} 预设生成图片成功。', 'reference_images': img_bytes_list})
            yield event.chain_result([Image.fromBytes(res), Plain(' | '.join(caption_parts))])
        else:
            event.set_extra('_mem_chat_shoubanhua_activity', {'status': 'failed', 'summary': f'{trigger_word} 图片生成失败，本次没有生成图片。', 'reference_images': img_bytes_list})
            yield event.plain_result(f'❌ 生成失败 ({elapsed:.2f}s)\n原因: {res}')

    async def _get_api_key(self) -> str | None:
        keys = self.conf.get('api_keys', [])
        if not keys:
            return None
        async with self.key_lock:
            key = keys[self.key_index]
            self.key_index = (self.key_index + 1) % len(keys)
            return key

    @staticmethod
    def _extract_image_reference_from_content(content: Any) -> str | None:
        """从 Chat Completions 的 content 中提取 URL 或 Base64 图片。"""
        if isinstance(content, str):
            data_uri_match = re.search('data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/_=\\r\\n-]+', content)
            if data_uri_match:
                return data_uri_match.group(0)
            url_match = re.search('https?://[^\\s<>")\\]]+', content)
            if url_match:
                return url_match.group(0).rstrip(')>,\'"')
            return None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    image_url = part.get('image_url')
                    if isinstance(image_url, dict):
                        image_url = image_url.get('url')
                    if isinstance(image_url, str) and image_url:
                        return image_url
                    ref = FigurineProPlugin._extract_image_reference_from_content(part.get('text'))
                    if ref:
                        return ref
        return None

    @staticmethod
    def _decode_data_image_uri(image_reference: str) -> bytes | None:
        if not image_reference.startswith('data:image/') or ',' not in image_reference:
            return None
        encoded = re.sub('\\s+', '', image_reference.split(',', 1)[1])
        if not encoded:
            return None
        padded = encoded + '=' * (-len(encoded) % 4)
        try:
            return base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            try:
                return base64.b64decode(padded, altchars=b'-_', validate=True)
            except (binascii.Error, ValueError):
                return None

    @staticmethod
    def _extract_images_api_reference(data: dict[str, Any]) -> str | None:
        images = data.get('data') if isinstance(data, dict) else None
        if not isinstance(images, list) or not images:
            return None
        first_image = images[0]
        if not isinstance(first_image, dict):
            return None
        b64_json = first_image.get('b64_json')
        if isinstance(b64_json, str) and b64_json.strip():
            mime_type = str(first_image.get('mime_type') or 'image/png').strip()
            if not mime_type.startswith('image/'):
                mime_type = 'image/png'
            return f'data:{mime_type};base64,{b64_json.strip()}'
        url = first_image.get('url')
        if isinstance(url, str) and url.strip():
            return url.strip()
        return None

    def _extract_image_url_from_response(self, data: dict[str, Any]) -> str | None:
        try:
            return data['choices'][0]['message']['images'][0]['image_url']['url']
        except (IndexError, TypeError, KeyError):
            pass
        try:
            return data['choices'][0]['message']['images'][0]['url']
        except (IndexError, TypeError, KeyError):
            pass
        try:
            content = data['choices'][0]['message']['content']
            return self._extract_image_reference_from_content(content)
        except (IndexError, TypeError, KeyError):
            pass
        return None

    async def _call_api(self, image_bytes_list: list[bytes], prompt: str, resolution: str | None=None, command_family: str='bnn') -> bytes | str | tuple[bytes, str]:
        backends = [backend for backend in self._build_remote_backends() if command_family == 'gpt' or backend.get('bnn_enabled', True)]
        if not backends:
            return '未配置可用的云端后端'
        if command_family == 'gpt' and self.conf.get('gpt_backend_round_robin', False):
            start = getattr(self, '_gpt_backend_cursor', 0) % len(backends)
            self._gpt_backend_cursor = (start + 1) % len(backends)
            backends = backends[start:] + backends[:start]
        failures: list[str] = []
        for backend in backends:
            keys = self._normalize_api_keys(backend.get('api_keys', []))
            if not keys:
                failures.append(f'{backend['name']} 未配置 API Key')
                continue
            runtime_models = self._ordered_backend_models(backend, command_family, resolution)
            if not runtime_models:
                failures.append(f'{backend['name']} 未配置可用模型')
                continue
            for key_index, api_key in enumerate(keys, start=1):
                probe_ok, probe_detail = await self._probe_backend(backend, api_key)
                if not probe_ok:
                    logger.warning(f'[手办化插件] {backend['name']} 第 {key_index} 个 Key 探活失败: {probe_detail}')
                    failures.append(f'{backend['name']} key{key_index} 探活失败: {probe_detail}')
                    continue
                for runtime_model in runtime_models:
                    logger.info(f'[手办化插件] 使用 {backend['name']} 第 {key_index} 个 Key，请求模型 {runtime_model}')
                    result = await self._call_api_with_backend(image_bytes_list=image_bytes_list, prompt=prompt, resolution=resolution, backend=backend, api_key=api_key, command_family=command_family, model_name=runtime_model)
                    if isinstance(result, bytes):
                        return (result, f'{backend['name']} / {runtime_model}')
                    logger.warning(f'[手办化插件] {backend['name']} 第 {key_index} 个 Key，模型 {runtime_model} 调用失败: {result}')
                    failures.append(f'{backend['name']} key{key_index} 模型 {runtime_model} 调用失败: {result}')
        return '；'.join(failures[:6]) or '所有后端调用均失败'

    async def _call_api_with_backend(self, image_bytes_list: list[bytes], prompt: str, resolution: str | None, backend: dict[str, Any], api_key: str, command_family: str='bnn', model_name: str | None=None) -> bytes | str:
        api_format = str(backend.get('api_format', 'openai') or 'openai').strip().lower()
        api_url = str(backend.get('api_url', '') or '').strip()
        if api_format != 'google' and (not api_url):
            return 'API URL 未配置'
        model_name = model_name or self._resolve_backend_model_name(backend, command_family, resolution)
        if api_format == 'google':
            google_base_url = self._derive_google_base_url(api_url, backend.get('google_api_base'))
            headers = {'Content-Type': 'application/json'}
            parts: list[dict[str, Any]] = [{'text': prompt}]
            for img_bytes in image_bytes_list:
                img_b64 = base64.b64encode(img_bytes).decode('utf-8')
                parts.append({'inline_data': {'mime_type': 'image/png', 'data': img_b64}})
            generation_config: dict[str, Any] = {'responseModalities': ['TEXT', 'IMAGE']}
            normalized_resolution = self._normalize_resolution(resolution, default='1k')
            _size_val, _image_size_param, aspect_ratio, _aspect_reason = self._choose_canvas_size(normalized_resolution, image_bytes_list, prompt)
            uses_google_image_size = 'gemini-3-pro-image-preview' in str(model_name) or 'gemini-3.1-flash-image-preview' in str(model_name)
            is_gemini_flash_image = 'gemini-2.5-flash-image' in str(model_name)
            if uses_google_image_size:
                image_size = '1K'
                if normalized_resolution == '2k':
                    image_size = '2K'
                elif normalized_resolution == '4k':
                    image_size = '4K'
                generation_config['imageConfig'] = {'imageSize': image_size, 'aspectRatio': aspect_ratio}
            elif is_gemini_flash_image:
                generation_config['imageConfig'] = {'aspectRatio': aspect_ratio}
            elif normalized_resolution == '2k':
                generation_config['mediaResolution'] = 'MEDIA_RESOLUTION_MEDIUM'
            elif normalized_resolution == '4k':
                generation_config['mediaResolution'] = 'MEDIA_RESOLUTION_HIGH'
            payload = {'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': generation_config}
            target_url = f'{google_base_url}/v1beta/models/{model_name}:generateContent'
            logger.info(f'调用 Google GenerateContent API: {target_url} | 模型: {model_name}')
            try:
                if not self.iwf:
                    return 'ImageWorkflow 未初始化'
                data = None
                async with self.iwf.session.post(target_url, params={'key': api_key}, json=payload, headers=headers, timeout=900) as resp:
                    has_highres_param = 'mediaResolution' in generation_config or 'imageConfig' in generation_config
                    if resp.status == 400 and normalized_resolution in {'2k', '4k'} and has_highres_param:
                        error_text = await resp.text()
                        logger.warning(f'Google API 400，尝试移除高清参数后重试: {error_text[:200]}')
                    elif resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f'Google API 请求失败: HTTP {resp.status}, 响应: {error_text}')
                        return f'API请求失败 (HTTP {resp.status}): {error_text[:200]}'
                    else:
                        data = await resp.json()
                has_highres_param = 'mediaResolution' in generation_config or 'imageConfig' in generation_config
                if data is None and normalized_resolution in {'2k', '4k'} and has_highres_param:
                    retry_generation_config = dict(generation_config)
                    retry_generation_config.pop('mediaResolution', None)
                    retry_generation_config.pop('imageConfig', None)
                    retry_payload = dict(payload)
                    retry_payload['generationConfig'] = retry_generation_config
                    async with self.iwf.session.post(target_url, params={'key': api_key}, json=retry_payload, headers=headers, timeout=900) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            logger.error(f'Google API 重试失败: HTTP {resp.status}, 响应: {error_text}')
                            return f'API请求失败 (HTTP {resp.status}): {error_text[:200]}'
                        data = await resp.json()
                candidates = data.get('candidates', []) if isinstance(data, dict) else []
                for cand in candidates:
                    content = cand.get('content') if isinstance(cand, dict) else None
                    parts_obj = content.get('parts') if isinstance(content, dict) else None
                    if not isinstance(parts_obj, list):
                        continue
                    for part in parts_obj:
                        if not isinstance(part, dict):
                            continue
                        inline_data = part.get('inline_data') or part.get('inlineData')
                        if isinstance(inline_data, dict):
                            b64_data = inline_data.get('data')
                            if isinstance(b64_data, str) and b64_data.strip():
                                return base64.b64decode(b64_data)
                        text_part = part.get('text')
                        if isinstance(text_part, str):
                            url_match = re.search('https?://[^\\s<>")\\]]+', text_part)
                            if url_match:
                                maybe_url = url_match.group(0).rstrip(')>,\'"')
                                img = await self.iwf._download_image(maybe_url)
                                if img:
                                    return img
                logger.error(f'Google API响应中未找到图片数据: {str(data)[:800]}')
                return f'API响应中未找到图片数据。原始响应 (部分): {str(data)[:500]}...'
            except asyncio.TimeoutError:
                logger.error('Google API 请求超时')
                return '请求超时，请稍后重试。'
            except aiohttp.ClientConnectorError as e:
                error_msg = f'连接失败: {e}。请检查网络环境。'
                logger.error(f'Google API 连接失败: {e}', exc_info=True)
                return error_msg
            except Exception as e:
                logger.error(f'调用 Google API 时发生未知错误: {e}', exc_info=True)
                return f'发生未知错误: {e}。'
        base_url = self._derive_openai_base_url(api_url, backend.get('blt_api_base'))
        if not base_url or self._is_disabled_backend_url(base_url):
            return 'API URL 未配置或后端已禁用'
        use_chat_completion = True
        use_image_generations = False
        use_image_edits = False
        gemini_image_prefers_generations = False
        if 'gpt-image' in model_name:
            if image_bytes_list:
                use_chat_completion = False
                use_image_edits = True
            else:
                use_chat_completion = False
                use_image_generations = True
        elif model_name.startswith('gemini-3.1-flash-image'):
            use_chat_completion = True
        elif 'nano-banana' in model_name:
            if image_bytes_list:
                use_chat_completion = False
                use_image_edits = True
            else:
                use_chat_completion = False
                use_image_generations = True
        elif 'doubao-seedream' in model_name or 'doubao' in model_name:
            use_chat_completion = False
            use_image_generations = True
            if image_bytes_list:
                logger.warning(f'模型 {model_name} 可能仅支持文生图，将忽略传入的参考图')
        headers = {'Authorization': f'Bearer {api_key}'}
        try:
            if not self.iwf:
                return 'ImageWorkflow 未初始化'
            if use_chat_completion:
                aspect_ratio = None
                ai_auto_aspect_ratio = bool(self.conf.get('gemini_ai_aspect_ratio', True))
                if image_bytes_list:
                    normalized_resolution = self._normalize_resolution(resolution, default='1k')
                    _size_val, _image_size_param, aspect_ratio, _aspect_reason = self._choose_canvas_size(normalized_resolution, image_bytes_list, prompt)
                elif not ai_auto_aspect_ratio:
                    normalized_resolution = self._normalize_resolution(resolution, default='1k')
                    _size_val, _image_size_param, aspect_ratio, _aspect_reason = self._choose_canvas_size(normalized_resolution, image_bytes_list, prompt)
                else:
                    logger.info('[手办化插件] 纯文生图未指定画布比例，交由 Gemini 自行判断')
                return await self._call_openai_chat_image_completion(base_url=base_url, model_name=model_name, prompt=prompt, image_bytes_list=image_bytes_list, headers=headers, aspect_ratio=aspect_ratio)
            elif use_image_edits:
                target_url = f'{base_url}/v1/images/edits'
                if 'gpt-image' in model_name:
                    gpt_auto_aspect_ratio = bool(self.conf.get('gpt_ai_aspect_ratio', True))
                    request_size = 'auto' if gpt_auto_aspect_ratio else '1024x1024'
                    logger.info(f'[手办化插件] GPT 图生图画布策略: size={request_size}')
                else:
                    normalized_resolution = self._normalize_resolution(resolution, default='1k')
                    size_val, image_size_param, aspect_ratio, _aspect_reason = self._choose_canvas_size(normalized_resolution, image_bytes_list, prompt)
                    request_size = size_val

                def build_form(prompt_text: str) -> aiohttp.FormData:
                    data = aiohttp.FormData()
                    if 'gpt-image' in model_name and len(image_bytes_list) > 1:
                        for idx, img_bytes in enumerate(image_bytes_list, start=1):
                            data.add_field('image[]', img_bytes, filename=f'image_{idx}.png', content_type='image/png')
                    else:
                        data.add_field('image', image_bytes_list[0], filename='image.png', content_type='image/png')
                    data.add_field('prompt', prompt_text)
                    data.add_field('model', model_name)
                    data.add_field('n', '1')
                    data.add_field('size', request_size)
                    if 'gpt-image' in model_name:
                        data.add_field('quality', self._gpt_image_quality())
                        data.add_field('response_format', 'b64_json')
                    else:
                        data.add_field('image_size', image_size_param)
                        data.add_field('aspect_ratio', aspect_ratio)
                    return data
                request_prompt = prompt
                if 'gpt-image' in model_name:
                    request_prompt = self._build_gpt_image_prompt(prompt, len(image_bytes_list))
                logger.info(f'调用 Image Edits API: {target_url} | 模型: {model_name}')
                result = await self._post_openai_formdata(target_url, build_form(request_prompt), headers)
                if isinstance(result, str) and '目前提示词无法驱动 ChatGPT 生成图片' in result and ('gpt-image' in model_name):
                    logger.warning('gpt-image 图片编辑首次提示词被拒，使用强化提示词重试一次')
                    retry_prompt = self._build_gpt_image_prompt(f'{prompt}\nMake the image request more concrete, visually explicit, and directly renderable.', len(image_bytes_list))
                    result = await self._post_openai_formdata(target_url, build_form(retry_prompt), headers)
                return result
            elif use_image_generations:
                target_url = f'{base_url}/v1/images/generations'
                headers['Content-Type'] = 'application/json'
                if 'gpt-image' in model_name:
                    gpt_auto_aspect_ratio = bool(self.conf.get('gpt_ai_aspect_ratio', True))
                    request_size = 'auto' if gpt_auto_aspect_ratio else '1024x1024'
                    logger.info(f'[手办化插件] GPT 文生图画布策略: size={request_size}')
                else:
                    normalized_resolution = self._normalize_resolution(resolution, default='1k')
                    size_val, image_size_param, aspect_ratio, _aspect_reason = self._choose_canvas_size(normalized_resolution, image_bytes_list, prompt)
                    if 'doubao' in model_name or 'seedream' in model_name:
                        size_val = '2048x2048'
                    request_size = size_val
                request_prompt = prompt
                if 'gpt-image' in model_name:
                    request_prompt = self._build_gpt_image_prompt(prompt, len(image_bytes_list))
                if 'gpt-image' in model_name:
                    payload = self._build_gpt_image_generation_payload(model_name, request_prompt, request_size, self._gpt_image_quality())
                else:
                    payload = {'prompt': request_prompt, 'model': model_name, 'n': 1, 'size': request_size}
                    payload['image_size'] = image_size_param
                    payload['aspect_ratio'] = aspect_ratio
                if model_name.startswith('gemini-3.1-flash-image') and image_bytes_list:
                    payload['image'] = [f'data:image/png;base64,{base64.b64encode(img_bytes).decode('utf-8')}' for img_bytes in image_bytes_list]
                if ('doubao' in model_name or 'seedream' in model_name) and image_bytes_list:
                    img_b64 = base64.b64encode(image_bytes_list[0]).decode('utf-8')
                    payload['image_url'] = f'data:image/png;base64,{img_b64}'
                    logger.info('Doubao 模型尝试发送 Base64 图片')
                logger.info(f'调用 Image Generations API: {target_url} | 模型: {model_name}')
                result = await self._post_openai_json(target_url, payload, headers)
                if isinstance(result, str) and gemini_image_prefers_generations:
                    logger.warning('[手办化插件] Gemini 图片 generations 接口失败，回退到 chat/completions 兼容路径')
                    result = await self._call_openai_chat_image_completion(base_url=base_url, model_name=model_name, prompt=prompt, image_bytes_list=image_bytes_list, headers=headers, aspect_ratio=aspect_ratio)
                if isinstance(result, str) and '目前提示词无法驱动 ChatGPT 生成图片' in result and ('gpt-image' in model_name):
                    logger.warning('gpt-image 文生图首次提示词被拒，使用强化提示词重试一次')
                    retry_payload = dict(payload)
                    retry_payload['prompt'] = self._build_gpt_image_prompt(f'{prompt}\nMake the image request more concrete, visually explicit, and directly renderable.', len(image_bytes_list))
                    result = await self._post_openai_json(target_url, retry_payload, headers)
                return result
        except asyncio.TimeoutError:
            logger.error('API 请求超时')
            return '请求超时，请稍后重试。'
        except aiohttp.ClientConnectorError as e:
            error_msg = f'连接失败: {e}。请检查网络环境。'
            logger.error(f'API 连接失败: {e}', exc_info=True)
            return error_msg
        except Exception as e:
            logger.error(f'调用 API 时发生未知错误: {e}', exc_info=True)
            return f'发生未知错误: {e}。'

    async def _handle_api_response(self, resp):
        if resp.status != 200:
            error_text = await resp.text()
            logger.error(f'API 请求失败: HTTP {resp.status}, 响应: {error_text}')
            try:
                error_data = json.loads(error_text)
                upstream_message = str((error_data.get('error') or {}).get('message') or '')
                if 'No available compatible accounts' in upstream_message:
                    return 'API 服务端没有可用的 GPT 图像上游账号。本地请求格式正确，请联系 api.yezi.help 管理员恢复生图渠道。'
            except (json.JSONDecodeError, AttributeError, TypeError):
                pass
            return f'API请求失败 (HTTP {resp.status}): {error_text[:200]}'
        data = await resp.json()
        if 'error' in data:
            return data['error'].get('message', json.dumps(data['error']))
        gen_image_url = self._extract_images_api_reference(data)
        if not gen_image_url:
            gen_image_url = self._extract_image_url_from_response(data)
        if not gen_image_url:
            error_msg = f'API响应中未找到图片数据。原始响应 (部分): {str(data)[:500]}...'
            logger.error(f'API响应中未找到图片数据: {str(data)[:500]}')
            return error_msg
        if gen_image_url.startswith('data:image/'):
            image_bytes = self._decode_data_image_uri(gen_image_url)
            if image_bytes:
                return image_bytes
            return 'API返回的 Base64 图片数据无效'
        else:
            return await self.iwf._download_image(gen_image_url) or '下载生成的图片失败'

    async def _post_openai_json(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> bytes | str:
        if not self.iwf:
            return 'ImageWorkflow 未初始化'
        async with self.iwf.session.post(url, json=payload, headers=headers, timeout=900) as resp:
            return await self._handle_api_response(resp)

    async def _post_openai_formdata(self, url: str, data: aiohttp.FormData, headers: dict[str, str]) -> bytes | str:
        if not self.iwf:
            return 'ImageWorkflow 未初始化'
        async with self.iwf.session.post(url, data=data, headers=headers, timeout=900) as resp:
            return await self._handle_api_response(resp)

    async def _call_openai_chat_image_completion(self, base_url: str, model_name: str, prompt: str, image_bytes_list: list[bytes], headers: dict[str, str], aspect_ratio: str | None=None) -> bytes | str:
        if not self.iwf:
            return 'ImageWorkflow 未初始化'
        target_url = f'{base_url}/v1/chat/completions'
        chat_headers = dict(headers)
        chat_headers['Content-Type'] = 'application/json'
        prompt_text = prompt
        if aspect_ratio:
            prompt_text = f'{prompt}\n\n输出画布比例要求: {aspect_ratio}。如果有参考图，优先保持与参考主图一致的长宽比，不要强行裁成 1:1。'
        messages_content = [{'type': 'text', 'text': prompt_text}]
        for img_bytes in image_bytes_list:
            img_b64 = base64.b64encode(img_bytes).decode('utf-8')
            messages_content.append({'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{img_b64}'}})
        payload = {'model': model_name, 'max_tokens': 1500, 'stream': False, 'messages': [{'role': 'user', 'content': messages_content}]}
        logger.info(f'调用 Chat Completion API: {target_url} | 模型: {model_name}')
        async with self.iwf.session.post(target_url, json=payload, headers=chat_headers, timeout=900) as resp:
            return await self._handle_api_response(resp)

    async def _call_local_zimage(self, image_bytes_list: list[bytes], prompt: str, resolution: str | None=None) -> bytes | str:
        try:
            url = str(self.conf.get('local_zimage_url', __import__('share_runtime').resolve('{{AB_ZIMAGE_LOCAL_LOCAL_ZIMAGE_URL}}')))
            normalized_resolution = self._normalize_resolution(resolution)
            size_val, _image_size_param, _aspect_ratio, _aspect_reason = self._choose_canvas_size(normalized_resolution, image_bytes_list, prompt)
            width_str, height_str = size_val.split('x', 1)
            width = int(width_str)
            height = int(height_str)
            payload: dict[str, Any] = {'prompt': prompt, 'width': width, 'height': height, 'steps': 6}
            if not self.iwf:
                return 'ImageWorkflow 未初始化'
            async with self.iwf.session.post(url, json=payload, timeout=900) as resp:
                if resp.status != 200:
                    t = await resp.text()
                    return f'本地服务错误 (HTTP {resp.status}): {t[:200]}'
                data = await resp.json()
            if not data.get('ok'):
                return data.get('error', '本地服务返回错误')
            try:
                img_b64 = data['image']
                return base64.b64decode(img_b64)
            except Exception as e:
                return f'解析本地服务返回图片失败: {e}'
        except Exception as e:
            return f'调用本地服务异常: {e}'

    async def terminate(self):
        if self.iwf:
            await self.iwf.terminate()
        logger.info('[FigurinePro] 插件已终止')
    _prefix_required = None

    @classmethod
    def set_prefix_config(cls, prefix):
        cls._prefix_required = prefix
        logger.info(f'[手办化插件] 设置prefix配置: {prefix}')

    @filter.regex(f'^({'|'.join([re.escape(trigger) for trigger in TRIGGER_WORDS])})\\s*.*')
    async def on_nano(self, event: AstrMessageEvent):
        logger.info(f'[手办化插件] on_nano函数被调用，接收到消息: {event.message_str}')
        logger.info(f'[手办化插件] 消息类型: {event.get_message_type()}, 发送者ID: {event.get_sender_id()}')
        logger.info(f'[手办化插件] 当前触发词列表: {TRIGGER_WORDS}')
        logger.info(f'[手办化插件] 当前正则表达式模式: {TRIGGER_PATTERN}')
        message_text = event.message_str.strip()
        logger.info(f'[手办化插件] 处理的消息文本: {message_text}')
        async for result in self._process_message(message_text, event):
            yield result

    @filter.regex(f'^({'|'.join([re.escape(trigger) for trigger in TRIGGER_WORDS])})\\s*.*')
    async def on_nano_without_prefix(self, event: AstrMessageEvent):
        if self._prefix_required:
            return
        logger.info(f'[手办化插件] on_nano_without_prefix函数被调用，接收到消息: {event.message_str}')
        logger.info(f'[手办化插件] 消息类型: {event.get_message_type()}, 发送者ID: {event.get_sender_id()}')
        logger.info(f'[手办化插件] 当前触发词列表: {TRIGGER_WORDS}')
        logger.info(f'[手办化插件] 当前正则表达式模式: {TRIGGER_PATTERN_WITHOUT_HASH}')
        async for result in self._process_message(event.message_str.strip(), event):
            yield result

    async def _process_message(self, message_text, event):
        trigger_word_match = re.match(f'^({TRIGGER_PATTERN_WITHOUT_HASH})\\s*', message_text, re.IGNORECASE)
        trigger_word = trigger_word_match.group(1) if trigger_word_match else None
        logger.info(f'[手办化插件] 匹配到的触发词: {trigger_word}')
        user_prompt = ''
        if trigger_word and message_text.lower().startswith(trigger_word.lower()):
            user_prompt = message_text[len(trigger_word):].strip()
        logger.info(f'[手办化插件] 用户提示词: {user_prompt}')
        if trigger_word == '手办化帮助':
            help_text = get_help_text()
            logger.info('[手办化插件] 处理手办化帮助命令')
            yield event.plain_result(help_text)
            return
        elif trigger_word == '手办化命令帮助':
            command_help_text = get_command_help()
            logger.info('[手办化插件] 处理手办化命令帮助命令')
            yield event.plain_result(command_help_text)
            return
        elif '手办化' in message_text or 'shoubanhua' in message_text.lower():
            if not trigger_word:
                trigger_word = '手办化1'
                logger.info(f'[手办化插件] 未找到精确触发词，使用默认: {trigger_word}')
            logger.info(f'[手办化插件] 调用_process_figurine_request，触发词: {trigger_word}, 用户提示词: {user_prompt}')
            async for result in self._process_figurine_request(event, trigger_word, user_prompt):
                yield result
            return
        elif trigger_word:
            logger.info(f'[手办化插件] 调用_process_figurine_request，触发词: {trigger_word}, 用户提示词: {user_prompt}')
            async for result in self._process_figurine_request(event, trigger_word, user_prompt):
                yield result
            return
        else:
            logger.info('[手办化插件] 未识别的命令，忽略')
        return
