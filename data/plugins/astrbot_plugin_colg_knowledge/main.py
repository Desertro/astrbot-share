import asyncio
import io
import json
import os
import re
import time
from urllib.parse import quote
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
try:
    import aiohttp
except Exception:
    aiohttp = None
try:
    from PIL import Image as _PILImage
except Exception:
    _PILImage = None
try:
    from bs4 import BeautifulSoup as _BeautifulSoup
    from readability import Document as _Document
except Exception:
    _Document = None
    _BeautifulSoup = None
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.io import save_temp_img
FORUM_ID_DEFAULT = 171
RATE_LIMIT_RPS = 0.5
EMBEDDING_RATE_LIMIT_RPS = 0.5
MAX_THREADS_PER_HOUR = 50
PAGES_PER_HOUR = 2
DAILY_DIGEST_HOUR = 3
CONFIRM_TOKEN = __import__('share_runtime').resolve('{{AB_COLG_KNOWLEDGE_CONFIRM_TOKEN}}')
MAX_THREAD_AGE_DAYS_DEFAULT = 3
PUSH_START_HOUR_BJ_DEFAULT = 8
TITLE_BLACKLIST = ['出', '收', '代练', '骗子', '咨询', '问个事', '求助', '小白', '有人吗', '进不去', '闪退', '怎么回事', '为什么', '新人', '求教']
MIN_CONTENT_LEN = 150
SNAPSHOT_USER_AGENT_DEFAULT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36'
MSHOTS_POLL_ATTEMPTS_DEFAULT = 6
MSHOTS_POLL_INTERVAL_SEC_DEFAULT = 5.0
_NOISE_TITLE_PATTERNS = ['招募', '工会', '加群', '群非常活跃', '开团时间']
_NOISE_TEXT_PATTERNS = ['本帖最后由', '在APP 编辑', '本帖隐藏的内容', '\\*{4,}', 'pan\\.', 'xunlei', 'weiyun', '夸克', '百度网盘', 'lanzou', '提取码', 'pwd=', 'QQ\\d{5,}', '\\bvx\\b', '微信', 'q群', '群号', '加群', '联系', '有人吗', '怎么回事', '为什么', '求助', '小白', '我就想问']
try:
    from zoneinfo import ZoneInfo
    _BJ_TZ = ZoneInfo('Asia/Shanghai')
except Exception:
    _BJ_TZ = timezone(timedelta(hours=8))

def _now_bj() -> datetime:
    return datetime.now(_BJ_TZ)

def _extract_main_post_datetime_bj(raw_html: str) -> datetime | None:
    if not raw_html:
        return None
    try:
        m = re.search('(20\\d{2})-(\\d{1,2})-(\\d{1,2})\\s+(\\d{1,2}):(\\d{2})\\s+发表于', raw_html)
        if not m:
            m = re.search('(20\\d{2})-(\\d{1,2})-(\\d{1,2})\\s+(\\d{1,2}):(\\d{2})', raw_html)
        if not m:
            return None
        y, mo, d, hh, mm = (int(x) for x in m.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=_BJ_TZ)
    except Exception:
        return None

def _get_state_dir() -> str:
    return os.path.join(get_astrbot_data_path(), 'plugin_data', 'astrbot_plugin_colg_knowledge')

def _get_state_path() -> str:
    return os.path.join(_get_state_dir(), 'state.json')

def _tidy_text(text: str) -> str:
    return (text or '').replace('\r', ' ').replace('\n', ' ').strip()

def _norm_for_dedup(text: str) -> str:
    s = _tidy_text(text)
    s = re.sub('\\s+', ' ', s).strip().lower()
    return s

def _is_noise_knowledge(title: str, text: str) -> bool:
    t = str(title or '').strip()
    s = _norm_for_dedup(text)
    if not s:
        return True
    for p in _NOISE_TITLE_PATTERNS:
        if re.search(p, t, re.I):
            return True
    for p in _NOISE_TEXT_PATTERNS:
        if re.search(p, s, re.I):
            return True
    return False

async def _fetch_text(url: str, headers: dict | None=None, timeout_sec: float=10) -> str:
    try:
        if aiohttp is not None:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, headers=headers or {}, timeout=timeout_sec) as resp:
                    if resp.status == 200:
                        return await resp.text(encoding='utf-8', errors='ignore')
                    logger.warning(f'[colg_knowledge] fetch text HTTP {resp.status}: {url}')
                    return ''

        def _fetch_sync() -> tuple[int, bytes]:
            req = urllib.request.Request(url, headers=headers or {}, method='GET')
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return (int(getattr(resp, 'status', 200) or 200), resp.read())
        status, data = await asyncio.to_thread(_fetch_sync)
        if status == 200:
            return data.decode('utf-8', errors='ignore')
        logger.warning(f'[colg_knowledge] fetch text HTTP {status}: {url}')
    except Exception as e:
        logger.warning(f'[colg_knowledge] fetch failed: {e}')
    return ''

async def _fetch_binary(url: str, headers: dict | None=None, timeout_sec: float=20) -> tuple[bytes, str]:
    try:
        if aiohttp is not None:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, headers=headers or {}, timeout=timeout_sec) as resp:
                    if resp.status != 200:
                        logger.warning(f'[colg_knowledge] fetch binary HTTP {resp.status}: {url}')
                        return (b'', '')
                    return (await resp.read(), str(resp.headers.get('Content-Type') or ''))

        def _fetch_sync() -> tuple[int, bytes, str]:
            req = urllib.request.Request(url, headers=headers or {}, method='GET')
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                status = int(getattr(resp, 'status', 200) or 200)
                content_type = str(resp.headers.get('Content-Type') or '')
                return (status, resp.read(), content_type)
        status, data, content_type = await asyncio.to_thread(_fetch_sync)
        if status != 200:
            logger.warning(f'[colg_knowledge] fetch binary HTTP {status}: {url}')
            return (b'', '')
        return (data, content_type)
    except Exception as e:
        logger.warning(f'[colg_knowledge] fetch binary failed: {e}')
        return (b'', '')

def _validate_snapshot_bytes(data: bytes, content_type: str='', expected_width: int=0) -> tuple[bool, str]:
    if not data:
        return (False, 'empty')
    if len(data) < 1024:
        return (False, f'too_small:{len(data)}')
    if _PILImage is None:
        if 'image/' not in str(content_type).lower():
            return (False, f'not_image:{content_type}')
        return (True, 'ok')
    try:
        with _PILImage.open(io.BytesIO(data)) as img:
            fmt = str(img.format or '').upper()
            width, height = img.size
            frames = int(getattr(img, 'n_frames', 1) or 1)
            if width <= 0 or height <= 0:
                return (False, 'invalid_dimensions')
            if fmt == 'GIF' and frames > 1:
                return (False, f'animated_gif_placeholder:{width}x{height}/{frames}')
            if fmt == 'GIF' and len(data) < 20000:
                return (False, f'gif_placeholder:{width}x{height}/{len(data)}')
            if expected_width >= 600 and width < max(1, int(expected_width * 0.75)):
                return (False, f'width_too_small:{width}<expected:{expected_width}')
            if fmt not in {'JPEG', 'JPG', 'PNG', 'WEBP'}:
                return (False, f'unsupported_format:{fmt or 'unknown'}')
            return (True, f'{fmt}:{width}x{height}/{len(data)}')
    except Exception as e:
        return (False, f'invalid_image:{e}')

def _save_snapshot_bytes(data: bytes) -> str:
    temp_dir = os.path.join(get_astrbot_data_path(), 'temp')
    os.makedirs(temp_dir, exist_ok=True)
    return save_temp_img(data)

async def _download_snapshot_to_temp(snapshot_url: str, *, expected_width: int, timeout_sec: float, user_agent: str, attempts: int, interval_sec: float) -> str:
    if not snapshot_url:
        return ''
    attempts = max(1, int(attempts or 1))
    interval_sec = max(0.0, float(interval_sec or 0.0))
    headers = {'User-Agent': user_agent or SNAPSHOT_USER_AGENT_DEFAULT, 'Accept': 'image/avif,image/webp,image/apng,image/png,image/jpeg,image/*,*/*;q=0.8', 'Cache-Control': 'no-cache'}
    last_reason = ''
    for attempt in range(1, attempts + 1):
        data, content_type = await _fetch_binary(snapshot_url, headers=headers, timeout_sec=timeout_sec)
        ok, reason = _validate_snapshot_bytes(data, content_type=content_type, expected_width=expected_width)
        if ok:
            try:
                path = _save_snapshot_bytes(data)
                logger.info(f'[colg_knowledge] snapshot downloaded: {reason} attempt={attempt}/{attempts}')
                return path
            except Exception as e:
                logger.warning(f'[colg_knowledge] snapshot save failed: {e}')
                return ''
        last_reason = reason
        if attempt < attempts:
            logger.info(f'[colg_knowledge] snapshot not ready ({reason}), retry {attempt}/{attempts}')
            await asyncio.sleep(interval_sec)
    logger.warning(f'[colg_knowledge] snapshot unavailable after {attempts} attempts: {last_reason}')
    return ''

def _extract_thread_id(url: str) -> str | None:
    m = re.search('(?:thread|tid)-(\\d+)', url)
    if m:
        return m.group(1)
    m = re.search('thread-(\\d+)-\\d+-\\d+\\\\.html', url)
    if m:
        return m.group(1)
    return None

def _extract_text_from_html(html: str) -> str:
    if _BeautifulSoup is not None:
        try:
            soup = _BeautifulSoup(html, 'html.parser')
            for tag in soup(['script', 'style', 'noscript']):
                tag.decompose()
            selectors = ['td.t_fsz', 'td.t_f', 'div.t_fsz', 'div.t_f', "div[id^='postmessage_']"]
            for sel in selectors:
                elem = soup.select_one(sel)
                if elem:
                    text = _tidy_text(elem.get_text(' ', strip=True))
                    if text:
                        return text
        except Exception:
            pass
    if _Document is not None and _BeautifulSoup is not None:
        try:
            doc = _Document(html)
            ret = doc.summary(html_partial=True)
            soup = _BeautifulSoup(ret, 'html.parser')
            return _tidy_text(soup.get_text())
        except Exception:
            pass
    try:
        import re as _re
        m = _re.search('class="t_fsz"[^>]*>([\\s\\S]*?)</td>', html)
        if m:
            text = _re.sub('<[^>]+>', ' ', m.group(1))
            return _tidy_text(text)
        text = _re.sub('<[^>]+>', ' ', html)
        return _tidy_text(text)
    except Exception:
        return _tidy_text(html)

def _make_knowledge_item(text: str, url: str, forum_id: int, thread_id: str) -> dict:
    content = text
    if len(content) > 500:
        content = content[:500].rstrip() + '…'
    content = f'{content}\n来源: {url}'
    import hashlib
    kid = hashlib.sha1((content + url).encode('utf-8')).hexdigest()
    return {'id': kid, 'text': content, 'url': url, 'source': 'colg', 'forum_id': forum_id, 'thread_id': thread_id, 'updated_at': int(time.time())}

def _build_snapshot_url(url: str, width: int) -> str:
    encoded = quote(url, safe='')
    if width > 0:
        return f'https://s0.wp.com/mshots/v1/{encoded}?w={width}'
    return f'https://s0.wp.com/mshots/v1/{encoded}'

async def _capture_snapshot_browser(url: str, width: int, height: int, timeout_sec: float, wait_sec: float, full_page: bool, user_agent: str) -> str:
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.warning(f'[colg_knowledge] playwright unavailable: {e}')
        return ''
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=['--no-sandbox'])
            context_kwargs: dict[str, Any] = {'viewport': {'width': width, 'height': height}}
            if user_agent:
                context_kwargs['user_agent'] = user_agent
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            await page.goto(url, wait_until='load', timeout=int(timeout_sec * 1000))
            if wait_sec > 0:
                await page.wait_for_timeout(int(wait_sec * 1000))
            image_bytes = await page.screenshot(full_page=full_page)
            await context.close()
            await browser.close()
            ok, reason = _validate_snapshot_bytes(image_bytes, content_type='image/png', expected_width=width)
            if not ok:
                logger.warning(f'[colg_knowledge] browser snapshot invalid: {reason}')
                return ''
            return _save_snapshot_bytes(image_bytes)
    except Exception as e:
        logger.warning(f'[colg_knowledge] browser snapshot failed: {e}')
        return ''

@register('colg_knowledge', 'desertro', 'COLG 论坛抓取与知识库同步插件', '0.1.0', '')
class Main(Star):
    TOOLS = ['colg_search']

    def __init__(self, context: Context, config: dict | None=None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._disabled = False
        try:
            stars = [s for s in self.context.get_all_stars() if s.name == 'colg_knowledge' and s.activated]
            self_module = self.__class__.__module__
            data_modules = [s.module_path for s in stars if s.module_path and s.module_path.startswith('data.plugins.')]
            if data_modules:
                if not self_module.startswith('data.plugins.'):
                    self._disabled = True
                else:
                    primary = sorted(data_modules)[0]
                    if self_module != primary:
                        self._disabled = True
        except Exception:
            self._disabled = False
        self.forum_id = self.config.get('forum_id', FORUM_ID_DEFAULT)
        self.rate_limit_rps = self.config.get('rate_limit_rps', RATE_LIMIT_RPS)
        embedding_rate_limit_rps = self.config.get('embedding_rate_limit_rps', EMBEDDING_RATE_LIMIT_RPS)
        try:
            self.embedding_rate_limit_rps = float(embedding_rate_limit_rps)
        except Exception:
            self.embedding_rate_limit_rps = EMBEDDING_RATE_LIMIT_RPS
        if self.embedding_rate_limit_rps < 0:
            self.embedding_rate_limit_rps = 0.0
        self.pages_per_hour = self.config.get('pages_per_hour', PAGES_PER_HOUR)
        self.max_threads_per_hour = self.config.get('max_threads_per_hour', MAX_THREADS_PER_HOUR)
        self.daily_digest_hour = self.config.get('daily_digest_hour', DAILY_DIGEST_HOUR)
        self.confirm_token = self.config.get('confirm_token', CONFIRM_TOKEN)
        self.crawl_interval_sec = int(self.config.get('crawl_interval_sec', 3600) or 3600)
        if self.crawl_interval_sec <= 0:
            self.crawl_interval_sec = 3600
        self.push_enabled = bool(self.config.get('push_enabled', False))
        self.push_interval_sec = int(self.config.get('push_interval_sec', 3600) or 3600)
        if self.push_interval_sec <= 0:
            self.push_interval_sec = 3600
        self.push_snapshot_enabled = bool(self.config.get('push_snapshot_enabled', False))
        self.snapshot_width = int(self.config.get('snapshot_width', 800) or 800)
        if self.snapshot_width < 200:
            self.snapshot_width = 200
        if self.snapshot_width > 1200:
            self.snapshot_width = 1200
        self.snapshot_mode = str(self.config.get('snapshot_mode', 'mshots')).lower()
        if self.snapshot_mode not in {'mshots', 'browser'}:
            self.snapshot_mode = 'mshots'
        self.snapshot_height = int(self.config.get('snapshot_height', 900) or 900)
        if self.snapshot_height < 200:
            self.snapshot_height = 200
        if self.snapshot_height > 3000:
            self.snapshot_height = 3000
        self.snapshot_wait_sec = float(self.config.get('snapshot_wait_sec', 1.5) or 1.5)
        if self.snapshot_wait_sec < 0:
            self.snapshot_wait_sec = 0.0
        self.snapshot_timeout_sec = float(self.config.get('snapshot_timeout_sec', 12) or 12)
        if self.snapshot_timeout_sec < 3:
            self.snapshot_timeout_sec = 3.0
        self.snapshot_mshots_poll_attempts = int(self.config.get('snapshot_mshots_poll_attempts', MSHOTS_POLL_ATTEMPTS_DEFAULT) or MSHOTS_POLL_ATTEMPTS_DEFAULT)
        if self.snapshot_mshots_poll_attempts < 1:
            self.snapshot_mshots_poll_attempts = 1
        if self.snapshot_mshots_poll_attempts > 12:
            self.snapshot_mshots_poll_attempts = 12
        self.snapshot_mshots_poll_interval_sec = float(self.config.get('snapshot_mshots_poll_interval_sec', MSHOTS_POLL_INTERVAL_SEC_DEFAULT) or MSHOTS_POLL_INTERVAL_SEC_DEFAULT)
        if self.snapshot_mshots_poll_interval_sec < 0:
            self.snapshot_mshots_poll_interval_sec = 0.0
        if self.snapshot_mshots_poll_interval_sec > 30:
            self.snapshot_mshots_poll_interval_sec = 30.0
        self.snapshot_full_page = bool(self.config.get('snapshot_full_page', True))
        self.snapshot_user_agent = str(self.config.get('snapshot_user_agent', SNAPSHOT_USER_AGENT_DEFAULT))
        self.group_whitelist = [str(g) for g in self.config.get('group_whitelist', []) if str(g).strip()]
        self._last_request_ts = 0.0
        self._last_embedding_ts = 0.0
        self._daily_items_cache: list[dict] = []
        self._group_sessions: dict[str, str] = {}
        self._pushed_item_ids: set[str] = set()
        self._today_date: str | None = None
        self._seen_thread_ids_today: set[str] = set()
        self._state_path = _get_state_path()
        self._last_push_bj_date: str = ''
        self._last_push_bj_ts: float = 0.0
        self._persist_pushed_item_ids: set[str] = set()
        self._load_state()
        if not self._disabled:
            asyncio.create_task(self._start_background_tasks())
        try:
            if not self._disabled:
                tmgr = self.context.get_llm_tool_manager()
                if isinstance(tmgr, FunctionToolManager):
                    tmgr.add_func(name='colg_search', func_args=[{'type': 'string', 'name': 'query', 'description': '查询关键词'}], desc='在 COLG 小时/日知识条目中进行快速检索（字符串匹配）', handler=self._tool_colg_search)
        except Exception as e:
            logger.warning(f'[colg_knowledge] register tool failed: {e}')

    def info(self) -> dict[str, Any]:
        return {'name': 'colg_knowledge', 'desc': 'COLG 论坛抓取与知识库同步', 'version': '0.1.0', 'author': 'desertro'}

    @filter.command('colg status')
    async def cmd_colg_status(self, event: AstrMessageEvent):
        if self._disabled:
            return
        mem_chat_ok = False
        try:
            mem_chat_meta = self.context.get_registered_star('mem_chat')
            mem_chat = mem_chat_meta.star_cls if mem_chat_meta else None
            mem_chat_ok = bool(mem_chat and hasattr(mem_chat, 'upsert_knowledge_items'))
        except Exception:
            mem_chat_ok = False
        log_path = str(Path(get_astrbot_data_path()) / 'logs' / 'colg_knowledge.log')
        try:
            gid = event.get_group_id()
            umo = getattr(event, 'unified_msg_origin', None)
            if gid and umo:
                self._group_sessions[str(gid)] = str(umo)
        except Exception:
            pass
        logger.info(f'[colg_knowledge] status forum_id={self.forum_id} pages={self.pages_per_hour} max_threads={self.max_threads_per_hour} mem_chat={mem_chat_ok}')
        yield event.plain_result(f'colg_knowledge OK\n- mem_chat 接口: {('OK' if mem_chat_ok else 'MISSING')}\n- 日志文件: {log_path}\n- forum_id: {self.forum_id}\n- pages_per_hour: {self.pages_per_hour}\n- max_threads_per_hour: {self.max_threads_per_hour}\n- rate_limit_rps: {self.rate_limit_rps}\n- embedding_rate_limit_rps: {self.embedding_rate_limit_rps}')

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if self._disabled:
            return
        gid = event.get_group_id()
        if not gid:
            return
        gid_str = str(gid)
        if self.group_whitelist and gid_str not in self.group_whitelist:
            return
        umo = getattr(event, 'unified_msg_origin', None)
        if umo:
            self._group_sessions[gid_str] = str(umo)

    async def _start_background_tasks(self):
        if self._disabled:
            return
        asyncio.create_task(self._hourly_loop())
        asyncio.create_task(self._daily_loop())
        asyncio.create_task(self._push_loop())

    async def _hourly_loop(self):
        while True:
            try:
                await self._hourly_sync()
            except Exception as e:
                logger.error(f'[colg_knowledge] hourly sync failed: {e}')
            await asyncio.sleep(self.crawl_interval_sec)

    async def _daily_loop(self):
        while True:
            try:
                now = datetime.now(timezone.utc)
                if now.hour == self.daily_digest_hour:
                    await self._daily_digest()
                    await asyncio.sleep(3600)
                else:
                    await asyncio.sleep(900)
            except Exception as e:
                logger.error(f'[colg_knowledge] daily loop error: {e}')
                await asyncio.sleep(900)

    async def _push_loop(self):
        while True:
            try:
                await self._maybe_push_to_groups()
            except Exception as e:
                logger.error(f'[colg_knowledge] push loop error: {e}')
            await asyncio.sleep(self.push_interval_sec)

    async def _respect_rate_limit(self):
        if self.rate_limit_rps <= 0:
            return
        min_interval = 1.0 / self.rate_limit_rps
        dt = time.time() - self._last_request_ts
        if dt < min_interval:
            await asyncio.sleep(min_interval - dt)
        self._last_request_ts = time.time()

    async def _respect_embedding_rate_limit(self):
        if self.embedding_rate_limit_rps <= 0:
            return
        min_interval = 1.0 / self.embedding_rate_limit_rps
        dt = time.time() - self._last_embedding_ts
        if dt < min_interval:
            await asyncio.sleep(min_interval - dt)
        self._last_embedding_ts = time.time()

    async def _hourly_sync(self):
        now = datetime.now(timezone.utc)
        today = now.strftime('%Y-%m-%d')
        if self._today_date != today:
            self._today_date = today
            self._seen_thread_ids_today.clear()
        base = f'https://bbs.colg.cn/forum-{self.forum_id}-{{page}}.html'
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36'}
        seen_items: list[dict] = []
        seen_content_keys: set[str] = set()
        threads_count = 0
        max_age_days = int(self.config.get('max_thread_age_days', MAX_THREAD_AGE_DAYS_DEFAULT) or MAX_THREAD_AGE_DAYS_DEFAULT)
        if max_age_days < 1:
            max_age_days = 1
        cutoff_ts = (_now_bj() - timedelta(days=max_age_days)).timestamp()
        for page in range(1, self.pages_per_hour + 1):
            if threads_count >= self.max_threads_per_hour:
                break
            url = base.format(page=page)
            await self._respect_rate_limit()
            html = await _fetch_text(url, headers=headers, timeout_sec=10)
            if not html:
                continue
            if _BeautifulSoup:
                soup = _BeautifulSoup(html, 'html.parser')
                thread_elements = soup.select("tbody[id^='normalthread_']")
                for element in thread_elements:
                    if threads_count >= self.max_threads_per_hour:
                        break
                    title_elem = element.select_one('a.xst')
                    if not title_elem:
                        continue
                    title = title_elem.get_text().strip()
                    th_url = title_elem.get('href')
                    num_elem = element.select_one('td.num a.xi2')
                    replies = int(num_elem.get_text()) if num_elem and num_elem.get_text().isdigit() else 0
                    if any((kw in title for kw in TITLE_BLACKLIST)):
                        continue
                    if replies < 5 and page > 1:
                        continue
                    if not th_url.startswith('http'):
                        th_url = 'https://bbs.colg.cn/' + th_url.lstrip('/')
                    thread_id = _extract_thread_id(th_url) or ''
                    if thread_id and thread_id in self._seen_thread_ids_today:
                        continue
                    await self._respect_rate_limit()
                    th_html = await _fetch_text(th_url, headers=headers, timeout_sec=10)
                    if not th_html:
                        continue
                    published_dt = _extract_main_post_datetime_bj(th_html)
                    if not published_dt:
                        continue
                    published_ts = published_dt.timestamp()
                    if published_ts < cutoff_ts:
                        continue
                    text = _extract_text_from_html(th_html)
                    if not text or len(text) < MIN_CONTENT_LEN:
                        continue
                    if text.count(' ') < 5 and len(text) < 300:
                        continue
                    if _is_noise_knowledge(title, text):
                        continue
                    content_key = _norm_for_dedup(text)[:800]
                    if content_key in seen_content_keys:
                        continue
                    seen_content_keys.add(content_key)
                    item = _make_knowledge_item(text, th_url, self.forum_id, thread_id)
                    item['title'] = title
                    item['thread_published_at'] = int(published_ts)
                    item['updated_at'] = int(published_ts)
                    seen_items.append(item)
                    threads_count += 1
                    if thread_id:
                        self._seen_thread_ids_today.add(thread_id)
            else:
                links = re.findall('href="([^"]*thread-\\d+-\\d+-\\d+\\.html)"', html)
                for th_url in list(set(links)):
                    if threads_count >= self.max_threads_per_hour:
                        break
                    full_url = th_url if th_url.startswith('http') else 'https://bbs.colg.cn/' + th_url.lstrip('/')
                    th_html = await _fetch_text(full_url, headers=headers)
                    published_dt = _extract_main_post_datetime_bj(th_html)
                    if not published_dt:
                        continue
                    published_ts = published_dt.timestamp()
                    if published_ts < cutoff_ts:
                        continue
                    text = _extract_text_from_html(th_html)
                    if text and len(text) >= MIN_CONTENT_LEN:
                        if _is_noise_knowledge('', text):
                            continue
                        content_key = _norm_for_dedup(text)[:800]
                        if content_key in seen_content_keys:
                            continue
                        seen_content_keys.add(content_key)
                        item = _make_knowledge_item(text, full_url, self.forum_id, '')
                        item['thread_published_at'] = int(published_ts)
                        item['updated_at'] = int(published_ts)
                        seen_items.append(item)
                        threads_count += 1
        if seen_items:
            await self._ingest_items(seen_items)
            self._daily_items_cache.extend(seen_items)
            if len(self._daily_items_cache) > 200:
                self._daily_items_cache = self._daily_items_cache[-200:]
            logger.info(f'[colg_knowledge] hourly ingested items: {len(seen_items)}')

    async def _daily_digest(self):
        if not self._daily_items_cache:
            logger.info('[colg_knowledge] no items for daily digest')
            return
        digest_input = '\n'.join([f'- 标题: {it.get('title', '无题')}\n  正文: {it.get('text', '')[:200]}...' for it in self._daily_items_cache[:30]])
        prompt = f'\n你是一个专业的游戏论坛内容分析师。下面是今天 COLG 论坛抓取到的部分热门帖子内容：\n\n{digest_input}\n\n请根据以上信息，写一份简短的“今日 COLG 论坛热点摘要”。\n要求：\n1. 重点关注职业平衡、版本改版、新活动、高价值攻略。\n2. 语言精炼，分条列出。\n3. 如果没有重要内容，请简单概括。\n4. 字数控制在 300 字以内。\n'
        try:
            provider = self.context.get_using_provider()
            if provider:
                resp = await self.context.llm_generate(chat_provider_id=provider.id, prompt=prompt, system_prompt='你是一个资深 DNF 玩家和论坛摘要助手。')
                summary = resp.completion_text
            else:
                summary = '今日 COLG 摘要：关注近期职业平衡、装备改版与活动更新。(LLM不可用)'
        except Exception as e:
            logger.error(f'[colg_knowledge] daily digest LLM failed: {e}')
            summary = '今日 COLG 摘要：关注近期职业平衡、装备改版与活动更新。(生成失败)'
        url = f'https://bbs.colg.cn/forum-{self.forum_id}-1.html'
        item = _make_knowledge_item(summary, url, self.forum_id, 'digest')
        await self._ingest_items([item])
        self._daily_items_cache.clear()
        logger.info('[colg_knowledge] daily digest generated with LLM')

    def _is_today_item(self, ts: Any) -> bool:
        if ts is None:
            return False
        try:
            dt = datetime.fromtimestamp(float(ts))
        except Exception:
            return False
        return dt.date() == datetime.now().date()

    async def _ingest_items(self, items: list[dict]):
        try:
            mem_chat_meta = self.context.get_registered_star('mem_chat')
        except Exception:
            mem_chat_meta = None
        mem_chat = mem_chat_meta.star_cls if mem_chat_meta else None
        if mem_chat and hasattr(mem_chat, 'upsert_knowledge_items'):
            try:
                mem_chat_any: Any = mem_chat
                if self.embedding_rate_limit_rps <= 0:
                    await mem_chat_any.upsert_knowledge_items(items, group_id='global')
                else:
                    for item in items:
                        await self._respect_embedding_rate_limit()
                        await mem_chat_any.upsert_knowledge_items([item], group_id='global')
                return
            except Exception as e:
                logger.warning(f'[colg_knowledge] interface ingest failed: {e}')
        try:
            knowledge_dir = os.path.join(get_astrbot_data_path(), 'plugins', 'astrbot_plugin_mem_chat', 'knowledge')
            os.makedirs(knowledge_dir, exist_ok=True)
            path = os.path.join(knowledge_dir, 'colg_hourly.md')
            with open(path, 'a', encoding='utf-8') as f:
                for it in items:
                    f.write(f'- {it.get('text', '')}\n\n')
        except Exception as e:
            logger.error(f'[colg_knowledge] file ingest failed: {e}')

    async def _maybe_push_to_groups(self):
        if not self.push_enabled:
            return
        if not self.group_whitelist:
            return
        now_bj = _now_bj()
        start_hour = int(self.config.get('push_start_hour_bj', PUSH_START_HOUR_BJ_DEFAULT) or PUSH_START_HOUR_BJ_DEFAULT)
        if start_hour < 0:
            start_hour = 0
        if start_hour > 23:
            start_hour = 23
        if now_bj.hour < start_hour:
            return
        today_bj = now_bj.strftime('%Y-%m-%d')
        push_once_per_day = bool(self.config.get('push_once_per_day', True))
        if push_once_per_day and self._last_push_bj_date == today_bj:
            logger.info(f'[colg_knowledge] push skipped: already pushed today (bj_date={today_bj})')
            return
        if not push_once_per_day:
            now_ts = float(now_bj.timestamp())
            try:
                min_interval = float(self.push_interval_sec or 0)
            except Exception:
                min_interval = 0.0
            if min_interval < 60:
                min_interval = 60.0
            if self._last_push_bj_ts and now_ts - float(self._last_push_bj_ts) < min_interval:
                logger.info(f'[colg_knowledge] push skipped: interval not reached (elapsed={now_ts - float(self._last_push_bj_ts):.0f}s < {min_interval:.0f}s)')
                return
        if not self._daily_items_cache:
            return
        max_age_days = int(self.config.get('max_thread_age_days', MAX_THREAD_AGE_DAYS_DEFAULT) or MAX_THREAD_AGE_DAYS_DEFAULT)
        if max_age_days < 1:
            max_age_days = 1
        cutoff_ts = (_now_bj() - timedelta(days=max_age_days)).timestamp()
        recent_items: list[dict] = []
        for it in self._daily_items_cache:
            ts = it.get('thread_published_at', None)
            if ts is None:
                ts = it.get('updated_at', None)
            try:
                if float(ts) >= cutoff_ts:
                    recent_items.append(it)
            except Exception:
                continue
        if not recent_items:
            return
        self._daily_items_cache = recent_items[-200:]
        mem_chat_persona: Any | None = None
        mem_chat_plain: Any | None = None
        try:
            mem_chat_meta = self.context.get_registered_star('mem_chat')
        except Exception:
            mem_chat_meta = None
        mem_chat = mem_chat_meta.star_cls if mem_chat_meta else None
        if mem_chat and hasattr(mem_chat, 'persona_share_text_items'):
            mem_chat_persona = mem_chat
        elif mem_chat and hasattr(mem_chat, 'share_text_to_session'):
            mem_chat_plain = mem_chat
        candidate: dict | None = None
        for it in reversed(self._daily_items_cache):
            iid = str(it.get('id') or '')
            if not iid:
                continue
            if iid in self._pushed_item_ids or iid in self._persist_pushed_item_ids:
                continue
            candidate = it
            break
        if not candidate:
            logger.info('[colg_knowledge] push skipped: no candidate item')
            return
        title = str(candidate.get('title') or '无题')
        text = str(candidate.get('text') or '')
        msg = f'【COLG 精选帖】{title}\n\n{text}'
        snapshot_url = ''
        snapshot_path = ''
        if self.push_snapshot_enabled:
            raw_url = str(candidate.get('url') or '').strip()
            if raw_url:
                if self.snapshot_mode == 'browser':
                    snapshot_path = await _capture_snapshot_browser(raw_url, self.snapshot_width, self.snapshot_height, self.snapshot_timeout_sec, self.snapshot_wait_sec, self.snapshot_full_page, self.snapshot_user_agent)
                    if not snapshot_path:
                        snapshot_url = _build_snapshot_url(raw_url, self.snapshot_width)
                        snapshot_path = await _download_snapshot_to_temp(snapshot_url, expected_width=self.snapshot_width, timeout_sec=max(self.snapshot_timeout_sec, 20.0), user_agent=self.snapshot_user_agent, attempts=self.snapshot_mshots_poll_attempts, interval_sec=self.snapshot_mshots_poll_interval_sec)
                else:
                    snapshot_url = _build_snapshot_url(raw_url, self.snapshot_width)
                    snapshot_path = await _download_snapshot_to_temp(snapshot_url, expected_width=self.snapshot_width, timeout_sec=max(self.snapshot_timeout_sec, 20.0), user_agent=self.snapshot_user_agent, attempts=self.snapshot_mshots_poll_attempts, interval_sec=self.snapshot_mshots_poll_interval_sec)
        mem_items = [{'id': str(candidate.get('id') or ''), 'title': title, 'text': text, 'url': str(candidate.get('url') or ''), 'source': 'colg'}]
        intro = '下面是从 COLG 论坛抓取到的一条推荐帖子，请用你的人格在群里自然地分享。'
        sent_any = False
        for gid in self.group_whitelist:
            gid_str = str(gid)
            session = self._group_sessions.get(gid_str)
            if not session:
                continue
            try:
                if mem_chat_persona is not None:
                    await mem_chat_persona.persona_share_text_items(session, mem_items, intro=intro, query=title or 'COLG 精选帖推荐')
                elif mem_chat_plain is not None:
                    await mem_chat_plain.share_text_to_session(session, msg)
                else:
                    await self.context.send_message(session, MessageEventResult().message(msg))
                if snapshot_path:
                    try:
                        img_result = MessageEventResult().file_image(snapshot_path)
                        await self.context.send_message(session, img_result)
                    except Exception as e:
                        logger.warning(f'[colg_knowledge] snapshot send failed for group {gid_str}: {e}')
                sent_any = True
            except Exception as e:
                logger.warning(f'[colg_knowledge] push failed for group {gid_str}: {e}')
        if sent_any:
            iid = str(candidate.get('id') or '')
            if iid:
                self._pushed_item_ids.add(iid)
                self._persist_pushed_item_ids.add(iid)
                if len(self._persist_pushed_item_ids) > 2000:
                    self._persist_pushed_item_ids = set(list(self._persist_pushed_item_ids)[-1000:])
                if len(self._pushed_item_ids) > 1000:
                    self._pushed_item_ids = set(list(self._pushed_item_ids)[-500:])
                self._last_push_bj_date = today_bj
                self._last_push_bj_ts = float(now_bj.timestamp())
                self._save_state()

    def _load_state(self) -> None:
        try:
            state_dir = _get_state_dir()
            os.makedirs(state_dir, exist_ok=True)
            path = self._state_path
            if not os.path.exists(path):
                return
            with open(path, 'r', encoding='utf-8') as f:
                data = json.loads(f.read() or '{}')
            if isinstance(data, dict):
                d = str(data.get('last_push_bj_date') or '').strip()
                if d:
                    self._last_push_bj_date = d
                try:
                    self._last_push_bj_ts = float(data.get('last_push_bj_ts') or 0.0)
                except Exception:
                    self._last_push_bj_ts = 0.0
                ids = data.get('pushed_item_ids', [])
                if isinstance(ids, list):
                    self._persist_pushed_item_ids = {str(x) for x in ids if str(x).strip()}
        except Exception:
            return

    def _save_state(self) -> None:
        try:
            state_dir = _get_state_dir()
            os.makedirs(state_dir, exist_ok=True)
            payload = {'last_push_bj_date': str(self._last_push_bj_date or ''), 'last_push_bj_ts': float(self._last_push_bj_ts or 0.0), 'pushed_item_ids': list(self._persist_pushed_item_ids)[-1000:], 'updated_at': int(time.time())}
            tmp = self._state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, self._state_path)
        except Exception:
            return

    async def _tool_colg_search(self, query: str) -> str:
        if self._disabled:
            return ''
        try:
            knowledge_dir = os.path.join(get_astrbot_data_path(), 'plugins', 'astrbot_plugin_mem_chat', 'knowledge')
            path = os.path.join(knowledge_dir, 'colg_hourly.md')
            if not os.path.exists(path):
                return '未找到 COLG 知识库文件。'
            results = []
            with open(path, encoding='utf-8') as f:
                content = f.read()
                items = content.split('\n\n')
                for it in items:
                    if query.lower() in it.lower():
                        results.append(it.strip())
            if not results:
                return f"未找到与 '{query}' 相关的 COLG 知识条目。"
            return '\n\n'.join(results[:5])
        except Exception as e:
            logger.error(f'[colg_knowledge] tool search failed: {e}')
            return f'检索失败: {e}'

    @filter.command('colg ingest')
    async def cmd_colg_ingest(self, event: AstrMessageEvent, pages: int=1, max_threads: int=20, confirm: str | None=None):
        if self._disabled:
            return
        if not confirm or confirm.strip() != self.confirm_token:
            yield event.plain_result(f'高风险操作，需确认口令。confirm={self.confirm_token}')
            return
        try:
            gid = event.get_group_id()
            umo = getattr(event, 'unified_msg_origin', None)
            if gid and umo:
                self._group_sessions[str(gid)] = str(umo)
        except Exception:
            pass
        pages = max(1, min(3, int(pages or 1)))
        max_threads = max(1, min(50, int(max_threads or 20)))
        self.pages_per_hour = pages
        self.max_threads_per_hour = max_threads
        await self._hourly_sync()
        yield event.plain_result(f'已抓取 pages={pages}, max_threads={max_threads}')
