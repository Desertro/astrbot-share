import asyncio
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
try:
    import aiohttp
except Exception:
    aiohttp = None
try:
    from bs4 import BeautifulSoup as _BeautifulSoup
    from readability import Document as _Document
except Exception:
    _Document = None
    _BeautifulSoup = None
from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
FORUM_ID_DEFAULT = 171
RATE_LIMIT_RPS = 0.5
MAX_THREADS_PER_HOUR = 50
PAGES_PER_HOUR = 2
DAILY_DIGEST_HOUR = 3
CONFIRM_TOKEN = __import__('share_runtime').resolve('{{AB_COLG_KNOWLEDGE_CONFIRM_TOKEN}}')
TITLE_BLACKLIST = ['出', '收', '代练', '骗子', '咨询', '问个事', '求助', '小白', '有人吗', '进不去', '闪退', '怎么回事', '为什么', '新人', '求教']
MIN_CONTENT_LEN = 150

def _tidy_text(text: str) -> str:
    return (text or '').replace('\r', ' ').replace('\n', ' ').strip()

async def _fetch_text(url: str, headers: dict | None=None, timeout_sec: float=10) -> str:
    if aiohttp is None:
        return ''
    try:
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.get(url, headers=headers or {}, timeout=timeout_sec) as resp:
                if resp.status == 200:
                    return await resp.text(encoding='utf-8', errors='ignore')
    except Exception as e:
        logger.warning(f'[colg_knowledge] fetch failed: {e}')
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

class Main(star.Star):
    TOOLS = ['colg_search']

    def __init__(self, context: star.Context, config: dict | None=None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._disabled = False
        try:
            stars = [s for s in self.context.get_all_stars() if s.name == 'colg_knowledge' and s.activated]
            self_module = self.__class__.__module__
            data_modules = [s.module_path for s in stars if s.module_path and s.module_path.startswith('data.plugins.')]
            if data_modules and (not self_module.startswith('data.plugins.')):
                self._disabled = True
        except Exception:
            self._disabled = False
        self.forum_id = self.config.get('forum_id', FORUM_ID_DEFAULT)
        self.rate_limit_rps = self.config.get('rate_limit_rps', RATE_LIMIT_RPS)
        self.pages_per_hour = self.config.get('pages_per_hour', PAGES_PER_HOUR)
        self.max_threads_per_hour = self.config.get('max_threads_per_hour', MAX_THREADS_PER_HOUR)
        self.daily_digest_hour = self.config.get('daily_digest_hour', DAILY_DIGEST_HOUR)
        self.confirm_token = self.config.get('confirm_token', CONFIRM_TOKEN)
        self._last_request_ts = 0.0
        self._daily_items_cache: list[dict] = []
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
        logger.info(f'[colg_knowledge] status forum_id={self.forum_id} pages={self.pages_per_hour} max_threads={self.max_threads_per_hour} mem_chat={mem_chat_ok}')
        yield event.plain_result(f'colg_knowledge OK\n- mem_chat 接口: {('OK' if mem_chat_ok else 'MISSING')}\n- 日志文件: {log_path}\n- forum_id: {self.forum_id}\n- pages_per_hour: {self.pages_per_hour}\n- max_threads_per_hour: {self.max_threads_per_hour}\n- rate_limit_rps: {self.rate_limit_rps}')

    async def _start_background_tasks(self):
        if self._disabled:
            return
        asyncio.create_task(self._hourly_loop())
        asyncio.create_task(self._daily_loop())

    async def _hourly_loop(self):
        while True:
            try:
                await self._hourly_sync()
            except Exception as e:
                logger.error(f'[colg_knowledge] hourly sync failed: {e}')
            await asyncio.sleep(3600)

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

    async def _respect_rate_limit(self):
        if self.rate_limit_rps <= 0:
            return
        min_interval = 1.0 / self.rate_limit_rps
        dt = time.time() - self._last_request_ts
        if dt < min_interval:
            await asyncio.sleep(min_interval - dt)
        self._last_request_ts = time.time()

    async def _hourly_sync(self):
        base = f'https://bbs.colg.cn/forum-{self.forum_id}-{{page}}.html'
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/121.0 Safari/537.36'}
        seen_items: list[dict] = []
        threads_count = 0
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
                    await self._respect_rate_limit()
                    th_html = await _fetch_text(th_url, headers=headers, timeout_sec=10)
                    if not th_html:
                        continue
                    text = _extract_text_from_html(th_html)
                    if not text or len(text) < MIN_CONTENT_LEN:
                        continue
                    if text.count(' ') < 5 and len(text) < 300:
                        continue
                    item = _make_knowledge_item(text, th_url, self.forum_id, thread_id)
                    item['title'] = title
                    seen_items.append(item)
                    threads_count += 1
            else:
                links = re.findall('href="([^"]*thread-\\d+-\\d+-\\d+\\.html)"', html)
                for th_url in list(set(links)):
                    if threads_count >= self.max_threads_per_hour:
                        break
                    full_url = th_url if th_url.startswith('http') else 'https://bbs.colg.cn/' + th_url.lstrip('/')
                    th_html = await _fetch_text(full_url, headers=headers)
                    text = _extract_text_from_html(th_html)
                    if text and len(text) >= MIN_CONTENT_LEN:
                        item = _make_knowledge_item(text, full_url, self.forum_id, '')
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

    async def _ingest_items(self, items: list[dict]):
        try:
            mem_chat_meta = self.context.get_registered_star('mem_chat')
        except Exception:
            mem_chat_meta = None
        mem_chat = mem_chat_meta.star_cls if mem_chat_meta else None
        if mem_chat and hasattr(mem_chat, 'upsert_knowledge_items'):
            try:
                mem_chat_any: Any = mem_chat
                await mem_chat_any.upsert_knowledge_items(items, group_id='global')
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
        pages = max(1, min(3, int(pages or 1)))
        max_threads = max(1, min(50, int(max_threads or 20)))
        self.pages_per_hour = pages
        self.max_threads_per_hour = max_threads
        await self._hourly_sync()
        yield event.plain_result(f'已抓取 pages={pages}, max_threads={max_threads}')
