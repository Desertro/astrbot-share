import asyncio
import base64
import contextlib
import io
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
import aiohttp
from PIL import Image as PILImage
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.log import LogManager
from astrbot.core.message.components import At, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
logger = LogManager.GetLogger('astrbot_plugin_zimage_local')

@register('astrbot_plugin_zimage_local', 'desertro', '本地Z-Image Turbo文生图/图生图插件', '0.1.0', 'https://local.z-image')
class LocalZImagePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.session: aiohttp.ClientSession | None = None
        self.local_session: aiohttp.ClientSession | None = None
        self.rate_lock = asyncio.Lock()
        self.global_hits: deque[float] = deque()
        self.user_hits: dict[str, deque[float]] = defaultdict(deque)
        self.user_last_ts: dict[str, float] = {}
        self.sem = self._build_semaphore()

    async def initialize(self):
        self.session = aiohttp.ClientSession(trust_env=True)
        self.local_session = aiohttp.ClientSession(trust_env=False)
        self.sem = self._build_semaphore()
        logger.info('[zimage_local] 插件已加载')

    def _build_semaphore(self):
        limit = int(self.conf.get('max_concurrency', 0) or 0)
        return asyncio.Semaphore(limit) if limit > 0 else None

    async def terminate(self):
        if self.session and (not self.session.closed):
            await self.session.close()
        if self.local_session and (not self.local_session.closed):
            await self.local_session.close()
        logger.info('[zimage_local] 插件已终止')

    def _is_global_admin(self, event: AstrMessageEvent) -> bool:
        admin_ids = self.context.get_config().get('admins_id', [])
        return str(event.get_sender_id()) in [str(x) for x in admin_ids]

    def _strip_command_prefix(self, text: str, command: str) -> str:
        if not text:
            return ''
        text = text.strip()
        if text.startswith(command):
            return text[len(command):].strip()
        if text.startswith(f'#{command}'):
            return text[len(command) + 1:].strip()
        return text

    def _extract_user_prompt(self, event: AstrMessageEvent, command: str) -> str:
        chain = getattr(getattr(event, 'message_obj', None), 'message', None)
        if chain:
            parts: list[str] = []
            for seg in chain:
                if isinstance(seg, Plain):
                    parts.append(seg.text)
            plain_text = ''.join(parts).strip()
            parsed = self._strip_command_prefix(plain_text, command)
            if parsed:
                return parsed
        return self._strip_command_prefix((event.message_str or '').strip(), command)

    async def _download(self, url: str, timeout: int=30) -> bytes | None:
        if not self.session:
            return None
        try:
            async with self.session.get(url, timeout=timeout) as resp:
                resp.raise_for_status()
                return await resp.read()
        except Exception as e:
            logger.warning(f'[zimage_local] 下载图片失败: {e}')
            return None

    async def _avatar(self, qq: str) -> bytes | None:
        if not qq.isdigit():
            return None
        return await self._download(f'https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640', timeout=15)

    def _extract_first_frame(self, raw: bytes) -> bytes:
        try:
            with PILImage.open(io.BytesIO(raw)) as img:
                if getattr(img, 'is_animated', False):
                    img.seek(0)
                    first = img.convert('RGB')
                    out = io.BytesIO()
                    first.save(out, format='PNG')
                    return out.getvalue()
        except Exception:
            pass
        return raw

    async def _load_image_bytes(self, src: str) -> bytes | None:
        raw: bytes | None = None
        loop = asyncio.get_running_loop()
        if Path(src).is_file():
            raw = await loop.run_in_executor(None, Path(src).read_bytes)
        elif src.startswith('http'):
            raw = await self._download(src)
        elif src.startswith('base64://'):
            raw = await loop.run_in_executor(None, base64.b64decode, src[9:])
        if not raw:
            return None
        return await loop.run_in_executor(None, self._extract_first_frame, raw)

    async def _collect_one_image(self, event: AstrMessageEvent) -> tuple[bytes, str] | None:
        chain = getattr(getattr(event, 'message_obj', None), 'message', [])
        for seg in chain:
            if isinstance(seg, Reply) and seg.chain:
                for s in seg.chain:
                    if isinstance(s, Image):
                        if s.url and (img := (await self._load_image_bytes(s.url))):
                            return (img, '引用图片')
                        if s.file and (img := (await self._load_image_bytes(s.file))):
                            return (img, '引用图片')
        for seg in chain:
            if isinstance(seg, Image):
                if seg.url and (img := (await self._load_image_bytes(seg.url))):
                    return (img, '消息图片')
                if seg.file and (img := (await self._load_image_bytes(seg.file))):
                    return (img, '消息图片')
        for seg in chain:
            if isinstance(seg, At):
                if (avatar := (await self._avatar(str(seg.qq)))):
                    return (avatar, '@头像')
        if bool(self.conf.get('allow_sender_avatar_fallback', False)):
            avatar = await self._avatar(str(event.get_sender_id()))
            if avatar:
                return (avatar, '发送者头像')
        return None

    async def _check_rate_limit(self, event: AstrMessageEvent) -> tuple[bool, str | None]:
        if not bool(self.conf.get('enable_rate_limit', True)):
            return (True, None)
        if bool(self.conf.get('admin_bypass_limit', True)) and self._is_global_admin(event):
            return (True, None)
        user_id = str(event.get_sender_id())
        now = time.monotonic()
        user_rpm = max(1, int(self.conf.get('user_rpm', 2)))
        global_rpm = max(1, int(self.conf.get('global_rpm', 8)))
        cooldown = max(0, int(self.conf.get('user_cooldown_seconds', 15)))
        async with self.rate_lock:
            while self.global_hits and now - self.global_hits[0] >= 60:
                self.global_hits.popleft()
            q = self.user_hits[user_id]
            while q and now - q[0] >= 60:
                q.popleft()
            last_ts = self.user_last_ts.get(user_id)
            if last_ts is not None and now - last_ts < cooldown:
                wait = int(cooldown - (now - last_ts) + 0.999)
                return (False, f'⏱️ 触发冷却中，请 {wait}s 后再试。')
            if len(self.global_hits) >= global_rpm:
                wait = int(60 - (now - self.global_hits[0]) + 0.999)
                return (False, f'🚦 当前本地生图请求较多，请 {wait}s 后重试。')
            if len(q) >= user_rpm:
                wait = int(60 - (now - q[0]) + 0.999)
                return (False, f'🚦 你当前请求过于频繁（每分钟上限 {user_rpm} 次），请 {wait}s 后重试。')
            self.global_hits.append(now)
            q.append(now)
            self.user_last_ts[user_id] = now
        return (True, None)

    def _pick_img2img_size(self, image_bytes: bytes) -> tuple[int, int]:
        max_side = int(self.conf.get('img2img_max_side', 0) or 0)
        min_side = 256
        try:
            with PILImage.open(io.BytesIO(image_bytes)) as im:
                w, h = im.size
        except Exception:
            w = int(self.conf.get('default_width', 512))
            h = int(self.conf.get('default_height', 512))
        if max_side > 0 and max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            w = int(w * scale)
            h = int(h * scale)
        w = max(min_side, w // 32 * 32)
        h = max(min_side, h // 32 * 32)
        return (w, h)

    async def _call_local_zimage(self, prompt: str, image_bytes: bytes | None=None) -> bytes | str:
        if not self.local_session:
            return 'HTTP session 未初始化'
        width = max(64, int(self.conf.get('default_width', 512)))
        height = max(64, int(self.conf.get('default_height', 512)))
        steps = max(1, int(self.conf.get('default_steps', 4)))
        guidance_scale = float(self.conf.get('guidance_scale', 0.0))
        strength = float(self.conf.get('img2img_strength', 0.45))
        timeout_cfg = int(self.conf.get('request_timeout_seconds', 0) or 0)
        timeout = timeout_cfg if timeout_cfg > 0 else None
        url = str(self.conf.get('local_zimage_url', __import__('share_runtime').resolve('{{AB_ZIMAGE_LOCAL_LOCAL_ZIMAGE_URL}}')))
        payload: dict[str, Any] = {'prompt': prompt, 'width': width, 'height': height, 'steps': steps, 'guidance_scale': guidance_scale}
        if image_bytes is not None:
            steps = max(1, int(self.conf.get('img2img_steps', 4)))
            width, height = self._pick_img2img_size(image_bytes)
            payload['width'] = width
            payload['height'] = height
            payload['steps'] = steps
            payload['image'] = base64.b64encode(image_bytes).decode('utf-8')
            payload['strength'] = max(0.0, min(1.0, strength))
        try:
            async with self.local_session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    t = await resp.text()
                    return f'本地服务错误 (HTTP {resp.status}): {t[:200]}'
                data = await resp.json()
        except asyncio.TimeoutError:
            return f'本地服务超时: 超过 {timeout}s 未返回（可调大 request_timeout_seconds 或降低图生图参数）'
        except Exception as e:
            return f'调用本地服务异常: {e}'
        if not data.get('ok'):
            err = str(data.get('error', 'unknown')).replace('\n', ' ').replace('\r', ' ').strip()
            if len(err) > 180:
                err = err[:177] + '...'
            return f'本地服务返回错误: {err}'
        try:
            return base64.b64decode(data['image'])
        except Exception as e:
            return f'解析图片失败: {e}'

    async def _run_generate(self, event: AstrMessageEvent, mode: str, prompt: str, image_bytes: bytes | None=None, image_source: str | None=None):
        max_prompt_chars = int(self.conf.get('max_prompt_chars', 0) or 0)
        prompt = (prompt or '').strip()
        if not prompt:
            yield event.plain_result('❌ 提示词不能为空。')
            return
        if max_prompt_chars > 0 and len(prompt) > max_prompt_chars:
            yield event.plain_result(f'❌ 提示词过长，当前上限 {max_prompt_chars} 字。')
            return
        ok, msg = await self._check_rate_limit(event)
        if not ok:
            yield event.plain_result(msg or '❌ 请求过于频繁')
            return
        mode_cn = '文生图' if mode == 'txt2img' else '图生图'
        yield event.plain_result(f'🎨 已接收请求，正在进行{mode_cn}...')
        start = time.monotonic()
        async with self.sem or contextlib.nullcontext():
            res = await self._call_local_zimage(prompt, image_bytes)
        elapsed = time.monotonic() - start
        if isinstance(res, bytes):
            suffix = f' | 来源: {image_source}' if image_source else ''
            yield event.chain_result([Image.fromBytes(res), Plain(f'✅ {mode_cn}完成 ({elapsed:.2f}s){suffix}')])
        else:
            err = str(res).replace('\n', ' ').replace('\r', ' ').strip()
            if len(err) > 180:
                err = err[:177] + '...'
            yield event.plain_result(f'❌ {mode_cn}失败 ({elapsed:.2f}s) 原因: {err}')

    @filter.command('z图帮助', prefix_optional=True)
    async def on_help(self, event: AstrMessageEvent):
        user_rpm = int(self.conf.get('user_rpm', 2))
        global_rpm = int(self.conf.get('global_rpm', 8))
        cooldown = int(self.conf.get('user_cooldown_seconds', 15))
        help_text = '🖼️ 本地 Z-Image 插件帮助\n- z画 <提示词>：文生图\n- z改 <提示词>：图生图（发送图/引用图/@用户头像）\n- z文生图 <提示词>：z画别名\n- z图生图 <提示词>：z改别名\n\n限流：已关闭（无限流、无冷却）'
        yield event.plain_result(help_text)

    @filter.command('z画', prefix_optional=True)
    async def on_txt2img(self, event: AstrMessageEvent):
        prompt = self._extract_user_prompt(event, 'z画')
        async for r in self._run_generate(event, 'txt2img', prompt):
            yield r

    @filter.command('z文生图', prefix_optional=True)
    async def on_txt2img_alias(self, event: AstrMessageEvent):
        prompt = self._extract_user_prompt(event, 'z文生图')
        async for r in self._run_generate(event, 'txt2img', prompt):
            yield r

    @filter.command('z改', prefix_optional=True)
    async def on_img2img(self, event: AstrMessageEvent):
        prompt = self._extract_user_prompt(event, 'z改')
        image_data = await self._collect_one_image(event)
        if not image_data:
            yield event.plain_result('❌ 图生图需要输入图：请发送图片、引用带图消息，或@一个用户。')
            return
        image_bytes, image_source = image_data
        async for r in self._run_generate(event, 'img2img', prompt, image_bytes, image_source):
            yield r

    @filter.command('z图生图', prefix_optional=True)
    async def on_img2img_alias(self, event: AstrMessageEvent):
        prompt = self._extract_user_prompt(event, 'z图生图')
        image_data = await self._collect_one_image(event)
        if not image_data:
            yield event.plain_result('❌ 图生图需要输入图：请发送图片、引用带图消息，或@一个用户。')
            return
        image_bytes, image_source = image_data
        async for r in self._run_generate(event, 'img2img', prompt, image_bytes, image_source):
            yield r
