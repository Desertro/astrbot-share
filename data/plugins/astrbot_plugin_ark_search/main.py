"""astrbot_plugin_ark_search

把火山方舟 (Volces ark) /api/v3/responses 接口包装成一个 LLM Function Tool，
让聊天 LLM (例如 DeepSeek v4) 可以在需要时直接调用「ark + web_search」拿到带搜索结果的回答。
也支持火山联网搜索产品的 APIKey 直连接口。

依赖：仅 aiohttp (随 AstrBot 一起带)。
"""
from __future__ import annotations
import asyncio
import base64
import html
import json
import mimetypes
import re
from typing import Any
import aiohttp
from astrbot import logger
from astrbot.api import llm_tool
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, register
DEFAULT_BASE_URL = __import__('share_runtime').resolve('{{AB_ARK_SEARCH_BASE_URL}}')
DEFAULT_FEEDCOOP_URL = 'https://open.feedcoopapi.com/search_api/web_search'
DEFAULT_MODEL = 'doubao-seed-2-0-mini-260215'
DEFAULT_REV_CONFIG = __import__('share_runtime').resolve('{{ASTRBOT_ROOT}}/data/config/astrbot_plugin_img_rev_searcher_config.json')
DEFAULT_SYSTEM_PROMPT = '你是一个负责检索网络信息的助手。请基于 web_search 工具返回的最新信息，给出 300 字以内的中文回答。\n- 必须显式调用 web_search 工具，至少 1 次，禁止仅凭记忆作答。\n- 如果是新闻/时效问题，请明确写出时间。\n- 如果搜不到可靠结果，直接说"未找到可靠信息"。\n- 不要输出 Markdown 标题与代码块，纯文本，分点可用「1. 2. 3.」。\n- 在结尾用一行列出主要来源（来源：网站名 ...），不要写完整 URL。'
SAUCENAO_LIKELY_MATCH = 70.0
SAUCENAO_STRONG_MATCH = 85.0

def _resolve_url(base_url: str) -> str:
    b = (base_url or DEFAULT_BASE_URL).rstrip('/')
    if b.endswith('/api/v3/responses'):
        return b
    if b.endswith('/api/v3'):
        return b + '/responses'
    return b + '/api/v3/responses'

def _extract_text(data: dict[str, Any]) -> str:
    """复用 vision_media 的 ark 响应解析逻辑。"""
    ot = data.get('output_text')
    if isinstance(ot, str) and ot.strip():
        return ot
    if isinstance(ot, list) and ot:
        joined = ''.join([str(x) for x in ot if isinstance(x, str)])
        if joined.strip():
            return joined
    pieces: list[str] = []
    for item in data.get('output', []) or []:
        if not isinstance(item, dict):
            continue
        if item.get('type') and item.get('type') != 'message':
            continue
        for c in item.get('content', []) or []:
            if not isinstance(c, dict):
                continue
            ctype = c.get('type') or ''
            if ctype in ('output_text', 'text'):
                t = c.get('text') or ''
                if isinstance(t, str) and t:
                    pieces.append(t)
    if pieces:
        return ''.join(pieces)
    msg = data.get('message') or {}
    if isinstance(msg, dict):
        mc = msg.get('content')
        if isinstance(mc, str):
            return mc
    return ''

def _extract_sources(data: dict[str, Any]) -> list[str]:
    """从 ark 返回里收集 web_search_call 的来源 url，用于附带在结果末尾。"""
    sources: list[str] = []
    seen: set[str] = set()
    for item in data.get('output', []) or []:
        if not isinstance(item, dict):
            continue
        if item.get('type') != 'web_search_call':
            continue
        results = (item.get('action') or {}).get('results') or item.get('results') or []
        if not isinstance(results, list):
            continue
        for r in results:
            if not isinstance(r, dict):
                continue
            url = r.get('url') or r.get('link') or ''
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(url)
            if len(sources) >= 5:
                return sources
    return sources

def _clip_text(text: str, limit: int) -> str:
    text = ' '.join((text or '').split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + '...'

def _data_url_to_bytes(data_url: str) -> tuple[bytes, str] | None:
    m = re.match('^data:([^;,]+)?;base64,(.+)$', data_url or '', re.S)
    if not m:
        return None
    mime = m.group(1) or 'image/jpeg'
    try:
        return (base64.b64decode(m.group(2), validate=False), mime)
    except Exception:
        return None

@register('ark_search', '皓月', 'Volces ark + web_search 包装成 FunctionTool，给聊天 LLM 提供联网搜索能力', '0.1.0', '')
class ArkSearch(Star):

    def __init__(self, context: Context, config: dict | None=None):
        super().__init__(context)
        self.config = config or {}

    def _api_mode(self) -> str:
        mode = str(self.config.get('api_mode') or 'ark_responses').strip().lower()
        if mode in {'feedcoop', 'feedcoop_api', 'search_api', 'direct'}:
            return 'feedcoop_api'
        return 'ark_responses'

    def _api_key(self) -> str:
        return str(self.config.get('api_key') or '').strip()

    def _base_url(self) -> str:
        return _resolve_url(str(self.config.get('base_url') or DEFAULT_BASE_URL))

    def _feedcoop_url(self) -> str:
        return str(self.config.get('feedcoop_url') or DEFAULT_FEEDCOOP_URL).strip() or DEFAULT_FEEDCOOP_URL

    def _model(self) -> str:
        return str(self.config.get('model') or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    def _system_prompt(self) -> str:
        p = str(self.config.get('system_prompt') or '').strip()
        return p or DEFAULT_SYSTEM_PROMPT

    def _max_output_tokens(self) -> int:
        try:
            v = int(self.config.get('max_output_tokens') or 2000)
        except Exception:
            v = 2000
        return max(256, v)

    def _timeout(self) -> int:
        try:
            v = int(self.config.get('timeout') or 60)
        except Exception:
            v = 60
        return max(10, v)

    def _enable_web_search(self) -> bool:
        v = self.config.get('enable_web_search')
        if v is None:
            return True
        return bool(v)

    def _feedcoop_count(self) -> int:
        try:
            v = int(self.config.get('feedcoop_count') or 5)
        except Exception:
            v = 5
        return min(10, max(1, v))

    def _feedcoop_search_type(self) -> str:
        st = str(self.config.get('feedcoop_search_type') or 'web').strip().lower()
        if st not in {'web', 'web_summary'}:
            return 'web'
        return st

    def _reverse_config(self) -> dict[str, Any]:
        cfg = self.config.get('reverse_search') or {}
        if isinstance(cfg, dict) and cfg:
            return cfg
        try:
            with open(DEFAULT_REV_CONFIG, 'r', encoding='utf-8-sig') as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _saucenao_settings(self) -> dict[str, Any]:
        rev = self._reverse_config()
        params = (rev.get('default_params') or {}).get('saucenao') or {}
        if not isinstance(params, dict):
            params = {}
        return {'api_key': str(self.config.get('saucenao_api_key') or params.get('api_key') or '').strip(), 'hide': int(params.get('hide') or 3), 'numres': int(params.get('numres') or 8), 'minsim': float(params.get('minsim') or 30)}

    async def _image_to_bytes(self, image_url: str) -> tuple[bytes, str, str] | None:
        raw = (image_url or '').strip()
        if not raw:
            return None
        data = _data_url_to_bytes(raw)
        if data:
            body, mime = data
            ext = mimetypes.guess_extension(mime) or '.jpg'
            return (body, mime, 'query' + ext)
        if raw.startswith('file://'):
            path = raw[7:]
            try:
                with open(path, 'rb') as f:
                    body = f.read()
                mime = mimetypes.guess_type(path)[0] or 'image/jpeg'
                return (body, mime, path.rsplit('/', 1)[-1] or 'query.jpg')
            except Exception as e:
                logger.error(f'[ark_search] read local image failed: {e}')
                return None
        if raw.startswith(('http://', 'https://')):
            try:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(raw, timeout=self._timeout()) as resp:
                        if resp.status != 200:
                            logger.error(f'[ark_search] download image HTTP {resp.status}')
                            return None
                        body = await resp.read()
                        mime = resp.headers.get('Content-Type', '').split(';', 1)[0]
                mime = mime or mimetypes.guess_type(raw)[0] or 'image/jpeg'
                return (body, mime, 'query' + (mimetypes.guess_extension(mime) or '.jpg'))
            except Exception as e:
                logger.error(f'[ark_search] download image failed: {e}')
                return None
        return None

    async def _call_saucenao(self, image_url: str, max_results: int=5) -> str:
        settings = self._saucenao_settings()
        api_key = settings.get('api_key')
        if not api_key:
            return 'Error: reverse_image_search 未配置 SauceNAO api_key。'
        image = await self._image_to_bytes(image_url)
        if not image:
            return 'Error: reverse_image_search 无法读取图片。'
        body, mime, filename = image
        if not body:
            return 'Error: reverse_image_search 图片为空。'
        params = {'output_type': '2', 'api_key': api_key, 'numres': str(max(1, min(10, max_results or settings['numres']))), 'hide': str(settings['hide']), 'minsim': str(settings['minsim'])}
        form = aiohttp.FormData()
        form.add_field('file', body, filename=filename, content_type=mime)
        headers = {'User-Agent': 'SauceNAO / Python client', 'Accept': 'application/json,text/plain,*/*'}
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post('https://saucenao.com/search.php', params=params, data=form, headers=headers, timeout=self._timeout()) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        logger.error(f'[ark_search] saucenao HTTP {resp.status}: {text[:300]}')
                        return f'Error: SauceNAO 返回 HTTP {resp.status}。'
                    try:
                        data = json.loads(text)
                    except Exception as e:
                        logger.error(f'[ark_search] saucenao JSON parse failed: {e}; raw={text[:300]}')
                        return 'Error: SauceNAO 返回无法解析。'
        except asyncio.TimeoutError:
            return f'Error: SauceNAO 接口超时（>{self._timeout()}s）。'
        except Exception as e:
            logger.error(f'[ark_search] saucenao request error: {e}')
            return f'Error: SauceNAO 请求失败 ({e})。'
        results = data.get('results') or []
        if not isinstance(results, list) or not results:
            return '未找到可靠的相似图片结果。'
        max_similarity = 0.0
        lines = ['反向搜图结果（SauceNAO，按相似度排序）：', f'判定规则：相似度 < {SAUCENAO_LIKELY_MATCH:.0f}% 不能确认出处；{SAUCENAO_LIKELY_MATCH:.0f}%-{SAUCENAO_STRONG_MATCH:.0f}% 只能说可能相关；>= {SAUCENAO_STRONG_MATCH:.0f}% 才可作为较强出处线索。']
        for idx, item in enumerate(results[:max(1, min(10, max_results))], start=1):
            if not isinstance(item, dict):
                continue
            header = item.get('header') or {}
            data_item = item.get('data') or {}
            sim = str(header.get('similarity') or '').strip()
            try:
                max_similarity = max(max_similarity, float(sim))
            except Exception:
                pass
            index_name = str(header.get('index_name') or '').strip()
            thumb = str(header.get('thumbnail') or '').strip()
            title = str(data_item.get('title') or data_item.get('source') or data_item.get('material') or data_item.get('eng_name') or '').strip()
            creator = data_item.get('creator') or data_item.get('member_name') or ''
            if isinstance(creator, list):
                creator = ', '.join([str(x) for x in creator if x])
            creator = str(creator or '').strip()
            urls = data_item.get('ext_urls') or []
            if isinstance(urls, str):
                urls = [urls]
            urls = [str(u).strip() for u in urls if str(u or '').strip()]
            lines.append(f'[{idx}] 相似度: {sim}% | 库: {index_name or 'unknown'}')
            if title:
                lines.append(f'标题/来源: {title}')
            if creator:
                lines.append(f'作者: {creator}')
            if urls:
                lines.append('页面: ' + '，'.join(urls[:3]))
            if thumb:
                lines.append(f'相关图片: {thumb}')
        if max_similarity >= SAUCENAO_STRONG_MATCH:
            lines.append(f'结论提示：最高相似度 {max_similarity:.1f}%，可作为较强出处线索，但仍需核对页面内容。')
        elif max_similarity >= SAUCENAO_LIKELY_MATCH:
            lines.append(f'结论提示：最高相似度 {max_similarity:.1f}%，只能说可能相关，不能直接断定真实出处。')
        else:
            lines.append(f'结论提示：最高相似度仅 {max_similarity:.1f}%，未找到可确认真实出处的匹配；请明确告诉用户“精确出处不确定”。')
        return '\n'.join(lines).strip()

    async def _call_duckduckgo_images(self, query: str, max_results: int=5) -> str:
        q = ' '.join((query or '').split())
        if not q:
            return ''
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36', 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}
        try:
            async with aiohttp.ClientSession(trust_env=True, headers=headers) as session:
                async with session.get('https://duckduckgo.com/', params={'q': q, 'iax': 'images', 'ia': 'images'}, timeout=self._timeout()) as resp:
                    page = await resp.text()
                m = re.search('vqd=([\\d-]+)&', page) or re.search('"vqd":"([^"]+)"', page)
                if not m:
                    return ''
                async with session.get('https://duckduckgo.com/i.js', params={'l': 'zh-cn', 'o': 'json', 'q': q, 'vqd': m.group(1), 'f': '', 'p': '1'}, headers={'Referer': 'https://duckduckgo.com/'}, timeout=self._timeout()) as resp:
                    text = await resp.text()
            data = json.loads(text)
        except Exception as e:
            logger.warning(f'[ark_search] duckduckgo image search failed: {e}')
            return ''
        results = data.get('results') or []
        if not isinstance(results, list) or not results:
            return ''
        lines = ['相关图片搜索结果（DuckDuckGo Images）：', '注意：这些是按关键词找到的相关图片/页面，不是反向搜图精确匹配，不能当作真实出处。']
        count = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            image = str(item.get('image') or '').strip()
            page_url = str(item.get('url') or '').strip()
            title = html.unescape(str(item.get('title') or '').strip())
            if not image:
                continue
            count += 1
            lines.append(f'[{count}] {title or '相关图片'}')
            lines.append(f'图片: {image}')
            if page_url:
                lines.append(f'页面: {page_url}')
            if count >= max(1, min(10, max_results)):
                break
        return '\n'.join(lines).strip() if count else ''

    async def _call_ark(self, query: str, image_url: str | None=None) -> str:
        api_key = self._api_key()
        if not api_key:
            return 'Error: ark_search 未配置 api_key，请在插件配置里填入。'
        url = self._base_url()
        content = [{'type': 'input_text', 'text': self._system_prompt()}, {'type': 'input_text', 'text': f'用户问题：{query}'}]
        image_url = (image_url or '').strip()
        if image_url:
            content.append({'type': 'input_image', 'image_url': image_url})
        payload: dict[str, Any] = {'model': self._model(), 'input': [{'role': 'user', 'content': content}], 'max_output_tokens': self._max_output_tokens()}
        if self._enable_web_search():
            payload['tools'] = [{'type': 'web_search'}]
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
        timeout = self._timeout()
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                    status = resp.status
                    text = await resp.text()
                    if status != 200:
                        logger.error(f'[ark_search] ark responses HTTP {status}: {text[:500]}')
                        return f'Error: ark 接口返回 HTTP {status}。'
                    try:
                        data = json.loads(text)
                    except Exception as e:
                        logger.error(f'[ark_search] JSON parse failed: {e}; raw={text[:500]}')
                        return 'Error: ark 返回无法解析。'
        except asyncio.TimeoutError:
            return f'Error: ark 接口超时（>{timeout}s）。'
        except Exception as e:
            logger.error(f'[ark_search] request error: {e}')
            return f'Error: ark 请求失败 ({e})。'
        content = _extract_text(data).strip()
        sources = _extract_sources(data)
        if not content:
            logger.warning(f'[ark_search] empty content, data keys={list(data.keys())}')
            return 'Error: ark 返回内容为空。'
        if sources:
            sources_line = '，'.join(sources)
            content = f'{content}\n\n[ark_search 参考链接] {sources_line}'
        return content

    async def _call_feedcoop(self, query: str, search_type: str | None=None) -> str:
        api_key = self._api_key()
        if not api_key:
            return 'Error: feedcoop search 未配置 api_key，请在插件配置里填入。'
        st = search_type or self._feedcoop_search_type()
        payload: dict[str, Any] = {'Query': query, 'SearchType': st, 'Count': self._feedcoop_count(), 'NeedSummary': True, 'Filter': {'NeedUrl': True}, 'QueryControl': {'QueryRewrite': False}}
        headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
        timeout = self._timeout()
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(self._feedcoop_url(), headers=headers, json=payload, timeout=timeout) as resp:
                    status = resp.status
                    text = await resp.text()
                    if status != 200:
                        logger.error(f'[ark_search] feedcoop HTTP {status}: {text[:500]}')
                        return f'Error: feedcoop 接口返回 HTTP {status}。'
                    try:
                        data = json.loads(text)
                    except Exception as e:
                        logger.error(f'[ark_search] feedcoop JSON parse failed: {e}; raw={text[:500]}')
                        return 'Error: feedcoop 返回无法解析。'
        except asyncio.TimeoutError:
            return f'Error: feedcoop 接口超时（>{timeout}s）。'
        except Exception as e:
            logger.error(f'[ark_search] feedcoop request error: {e}')
            return f'Error: feedcoop 请求失败 ({e})。'
        meta = data.get('ResponseMetadata') or {}
        err = meta.get('Error')
        if err:
            code = str(err.get('Code') or err.get('CodeN') or '')
            msg = str(err.get('Message') or 'unknown error')
            if st == 'web_summary' and code == '10409':
                logger.warning('[ark_search] feedcoop web_summary unsupported, fallback to web')
                return await self._call_feedcoop(query, search_type='web')
            logger.error(f'[ark_search] feedcoop error {code}: {msg}')
            return f'Error: feedcoop 接口错误 {code}: {msg}'
        result = data.get('Result') or {}
        choices = result.get('Choices') or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            msg = choice.get('Message') or {}
            content = msg.get('Content') or msg.get('content')
            if isinstance(content, str) and content.strip():
                return content.strip()
        items = result.get('WebResults') or []
        if not items:
            return '未找到可靠信息。'
        lines = ['[feedcoop_search 搜索结果]']
        sources: list[str] = []
        for idx, item in enumerate(items[:self._feedcoop_count()], start=1):
            if not isinstance(item, dict):
                continue
            title = _clip_text(str(item.get('Title') or '无标题'), 80)
            site = _clip_text(str(item.get('SiteName') or ''), 30)
            pub = _clip_text(str(item.get('PublishTime') or ''), 25)
            summary = _clip_text(str(item.get('Summary') or item.get('Snippet') or ''), 420)
            url = str(item.get('Url') or '').strip()
            source = f'（{site}）' if site else ''
            time_part = f' [{pub}]' if pub else ''
            lines.append(f'{idx}. {title}{source}{time_part}\n{summary}')
            if url:
                sources.append(url)
        if sources:
            lines.append('[feedcoop_search 参考链接] ' + '，'.join(sources[:5]))
        return '\n'.join(lines).strip()

    @llm_tool(name='ark_web_search')
    async def ark_web_search(self, event: AstrMessageEvent, query: str, image_url: str='', image_ref: str='', include_images: bool=False) -> str:
        """搜索网络以获取最新信息。当用户的问题涉及实时新闻、近期发生的事件、
        某个具体作品/角色/人物的最新动态、需要联网才能确认的事实时，调用此工具。
        如果用户给了图片直链，也可先识别图片，再结合 web_search 搜索相关信息。
        本工具会调用火山方舟豆包 + web_search，或火山联网搜索 API，返回可直接转述给用户的搜索结果。

        Args:
            query(string): 想要在网上搜索的关键词或自然语言问题。请用中文。
            image_url(string): 可选。图片直链；只有 ark_responses 模式支持。需要识图搜图时填写。
            image_ref(string): 可选。由上游 mem_chat 解析的历史图片引用，例如 IMGREF:1。
            include_images(bool): 可选。用户想要图片、表情包、动图、插画、壁纸等可发送图片候选时设为 true。

        """
        q = (query or '').strip()
        if not q:
            return 'Error: ark_web_search 收到空 query。'
        mode = self._api_mode()
        logger.info(f'[ark_search] tool call mode={mode} query={q!r}')
        if mode == 'feedcoop_api':
            result = await self._call_feedcoop(q)
        else:
            result = await self._call_ark(q, image_url=image_url)
        if include_images:
            image_result = await self._call_duckduckgo_images(q, max_results=5)
            if image_result:
                result = f'{result.rstrip()}\n\n{image_result}' if result else image_result
        logger.info(f'[ark_search] tool result len={len(result)}')
        return result

    @llm_tool(name='reverse_image_search')
    async def reverse_image_search(self, event: AstrMessageEvent, image_url: str='', image_ref: str='', query: str='', max_results: int=5) -> str:
        """反向搜图，查找一张图片的相似图片、可能出处、来源页面。
        当用户要求“以图搜图 / 搜相关图片 / 查相似图 / 查出处 / 反搜图片”时优先调用本工具。
        返回结果中包含页面链接与可直接发送的相关图片缩略图 URL。

        Args:
            image_url(string): 图片直链或 data:image/base64。当前消息有图时应填写。
            image_ref(string): 可选。历史图片引用，例如 IMGREF:1。
            query(string): 可选。只填写用户原话或图片上可见文字，不要自行补角色名、作者名或作品名。
            max_results(int): 可选。返回结果数，默认 5。
        """
        logger.info(f'[ark_search] reverse image search query={query!r} image_ref={image_ref!r}')
        if not (image_url or '').strip():
            return 'Error: reverse_image_search 收到空 image_url。'
        parts: list[str] = []
        ddg_result = await self._call_duckduckgo_images(query, max_results=max_results)
        if ddg_result:
            parts.append(ddg_result)
        saucenao_result = await self._call_saucenao(image_url, max_results=max_results)
        if saucenao_result:
            parts.append(saucenao_result)
        result = '\n\n'.join(parts).strip() or '未找到可靠的相关图片结果。'
        logger.info(f'[ark_search] reverse image result len={len(result)}')
        return result
