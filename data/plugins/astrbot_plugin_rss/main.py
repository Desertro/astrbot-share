import asyncio
import hashlib
import logging
import re
import time
import ssl
import certifi
import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from lxml import etree
from aiohttp import web
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from .data_handler import DataHandler
from .pic_handler import RssImageHandler
from .rss import RSSItem

@register('astrbot_plugin_rss', 'Soulter', 'RSS订阅插件', '1.1.0', 'https://github.com/Soulter/astrbot_plugin_rss')
class RssPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.logger = logging.getLogger('astrbot')
        self.context = context
        self.config = config
        self.data_handler = DataHandler()
        self.pic_handler = RssImageHandler()
        self.title_max_length = config.get('title_max_length')
        self.description_max_length = config.get('description_max_length')
        self.max_items_per_poll = config.get('max_items_per_poll')
        self.t2i = config.get('t2i')
        self.is_hide_url = config.get('is_hide_url')
        self.is_read_pic = config.get('pic_config').get('is_read_pic')
        self.is_adjust_pic = config.get('pic_config').get('is_adjust_pic')
        self.max_pic_item = config.get('pic_config').get('max_pic_item')
        self.is_compose = config.get('compose')
        self.use_parser = config.get('use_parser')
        self.default_rsshub_endpoint = (config.get('default_rsshub_endpoint') or '').strip()
        self.rsshub_access_key = (config.get('rsshub_access_key') or '').strip()
        self.rsshub_whitelist_sessions = config.get('rsshub_whitelist_sessions') or []
        if not isinstance(self.rsshub_whitelist_sessions, list):
            self.rsshub_whitelist_sessions = []
        settings = self.data_handler.data.get('settings') or {}
        if not self.rsshub_access_key:
            self.rsshub_access_key = (settings.get('rsshub_access_key') or '').strip()
        if not self.rsshub_whitelist_sessions and isinstance(settings.get('rsshub_whitelist_sessions'), list):
            self.rsshub_whitelist_sessions = settings.get('rsshub_whitelist_sessions')
        self.pic_handler = RssImageHandler(self.is_adjust_pic)
        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        self._channel_fetch_cache: dict[str, tuple[float, bytes | None]] = {}
        self._channel_fetch_locks: dict[str, asyncio.Lock] = {}
        self._channel_fetch_cache_ttl = 20
        self._fresh_asyncIOScheduler()
        try:
            asyncio.create_task(self._apply_group_subscriptions_config_async())
        except Exception:
            pass
        try:
            asyncio.create_task(self._start_manage_ui())
        except Exception:
            pass

    def _load_cmd_config(self) -> dict | None:
        try:
            root = Path(__file__).resolve().parents[3]
            cmd_config = root / 'data' / 'cmd_config.json'
            if not cmd_config.exists():
                return None
            import json
            return json.loads(cmd_config.read_text(encoding='utf-8-sig'))
        except Exception:
            return None

    def _core_whitelist_allows(self, session: str, group_id: str | None=None) -> bool:
        conf = self._load_cmd_config()
        if not conf:
            return True
        ps = conf.get('platform_settings') or {}
        enable = bool(ps.get('enable_id_white_list'))
        if not enable:
            return True
        whitelist = ps.get('id_whitelist') or []
        whitelist = [str(i).strip() for i in whitelist if str(i).strip()]
        if not whitelist:
            return True
        if session in whitelist:
            return True
        if group_id and str(group_id).strip() in whitelist:
            return True
        return False

    def _get_core_whitelisted_group_ids(self) -> list[str]:
        conf = self._load_cmd_config()
        if not conf:
            return []
        ps = conf.get('platform_settings') or {}
        enable = bool(ps.get('enable_id_white_list'))
        whitelist = ps.get('id_whitelist') or []
        whitelist = [str(i).strip() for i in whitelist if str(i).strip()]
        if not enable or not whitelist:
            return []
        group_ids: set[str] = set()
        for item in whitelist:
            if item.isdigit():
                group_ids.add(item)
                continue
            parts = item.split(':')
            if len(parts) >= 3 and parts[-2] == 'GroupMessage' and parts[-1].isdigit():
                group_ids.add(parts[-1])
        return sorted(group_ids)

    def _build_group_session(self, platform_id: str, group_id: str) -> str:
        return f'{platform_id}:GroupMessage:{group_id}'

    def _should_bypass_proxy(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            host = parts.hostname
            if not host:
                return False
            if host in {'localhost', '127.0.0.1', '::1'}:
                return True
            try:
                ip = ipaddress.ip_address(host)
                return ip.is_loopback or ip.is_private or ip.is_link_local
            except ValueError:
                return False
        except Exception:
            return False

    def _is_rsshub_session_allowed(self, session: str) -> bool:
        if not self.rsshub_whitelist_sessions:
            return True
        if session in self.rsshub_whitelist_sessions:
            return True
        parts = session.split(':')
        group_id = parts[-1] if len(parts) >= 3 else ''
        if group_id and str(group_id) in {str(s).strip() for s in self.rsshub_whitelist_sessions}:
            return True
        return False

    def _with_rsshub_access_key(self, url: str, session: str) -> str:
        if not self.rsshub_access_key:
            return url
        if not self._is_rsshub_session_allowed(session):
            return url
        try:
            parts = urlsplit(url)
            query_items = parse_qsl(parts.query, keep_blank_values=True)
            keys = {k for k, _ in query_items}
            if 'key' in keys or 'code' in keys:
                return url
            query_items.append(('key', self.rsshub_access_key))
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))
        except Exception:
            return url

    def _construct_subscription_url(self, idx_or_url: str, route: str, session: str) -> tuple[str | None, str | None]:
        raw_value = (idx_or_url or '').strip()
        if raw_value.startswith('`') and raw_value.endswith('`'):
            raw_value = raw_value[1:-1].strip()
        if self._is_url_or_ip(raw_value):
            uid = self._extract_bilibili_uid(raw_value)
            if uid:
                if not self._is_rsshub_session_allowed(session):
                    return (None, '当前会话不在 RSSHub 白名单')
                endpoint = self._get_default_rsshub_endpoint()
                url = f'{endpoint}/bilibili/user/video/{uid}?format=rss'
                return (self._with_rsshub_access_key(url, session), None)
            return (raw_value, None)
        if route == '':
            return (None, '路由不能为空, 请使用 /rss add <idx> <route> <cron_expr>')
        if not self._is_rsshub_session_allowed(session):
            return (None, '当前会话不在 RSSHub 白名单')
        try:
            idx = int(raw_value)
        except ValueError:
            return (None, '请输入正确的 RSSHub 站点索引或 URL')
        endpoints = self.data_handler.data['rsshub_endpoints']
        if len(endpoints) == 0:
            return (None, '请先使用 /rss rsshub add <url> 添加 RSSHub 端点')
        if idx < 0 or idx >= len(endpoints):
            return (None, '索引越界, 请使用 /rss rsshub list 查看已经添加的端点')
        if not route.startswith('/'):
            return (None, '路由必须以 / 开头')
        base = self._origin_of(endpoints[idx]).rstrip('/')
        url = base + route
        return (self._with_rsshub_access_key(url, session), None)

    def parse_cron_expr(self, cron_expr: str):
        fields = cron_expr.split(' ')
        return {'minute': fields[0], 'hour': fields[1], 'day': fields[2], 'month': fields[3], 'day_of_week': fields[4]}

    def _job_second_offset(self, url: str, user: str) -> int:
        digest = hashlib.md5(f'{url}|{user}'.encode('utf-8')).hexdigest()
        return int(digest[:8], 16) % 60

    def _get_channel_lock(self, url: str) -> asyncio.Lock:
        lock = self._channel_fetch_locks.get(url)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_fetch_locks[url] = lock
        return lock

    def _get_parser_plugin(self):
        star = self.context.get_registered_star('astrbot_plugin_parser')
        if not star or not star.activated or star.star_cls is None:
            return None
        return star.star_cls

    async def _get_parser_components(self, url: str, session: str):
        if not self.use_parser:
            return []
        parser = self._get_parser_plugin()
        if not parser:
            return []
        disabled_sessions = []
        if getattr(parser, 'config', None):
            disabled_sessions = parser.config.get('disabled_sessions') or []
        if session and session in disabled_sessions:
            return []
        try:
            return await parser.parse_url_to_components(url, session=session)
        except Exception as e:
            self.logger.error(f'rss: 解析链接失败: {url} - {str(e)}')
            return []

    async def _send_parser_followup(self, url: str, session: str):
        parser_comps = await self._get_parser_components(url, session)
        if not parser_comps:
            return
        msc = MessageChain(chain=parser_comps, use_t2i_=self.t2i)
        await self.context.send_message(session, msc)

    async def parse_channel_info(self, url):
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        now = time.time()
        cached = self._channel_fetch_cache.get(url)
        if cached and now - cached[0] < self._channel_fetch_cache_ttl:
            return cached[1]
        lock = self._get_channel_lock(url)
        async with lock:
            now = time.time()
            cached = self._channel_fetch_cache.get(url)
            if cached and now - cached[0] < self._channel_fetch_cache_ttl:
                return cached[1]
            text = await self._fetch_channel_info(url, headers)
            self._channel_fetch_cache[url] = (time.time(), text)
            return text

    async def _fetch_channel_info(self, url, headers):
        try:
            try:
                import aiohttp as _aiohttp
            except Exception:
                _aiohttp = None
            if _aiohttp is not None:
                connector = _aiohttp.TCPConnector(ssl=False)
                timeout = _aiohttp.ClientTimeout(total=30, connect=10)
                bypass_proxy = self._should_bypass_proxy(url)
                trust_env = not bypass_proxy
                self.logger.debug(f'rss: 请求站点 {url}, bypass_proxy={bypass_proxy}, trust_env={trust_env}')
                async with _aiohttp.ClientSession(trust_env=trust_env, connector=connector, timeout=timeout, headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            self.logger.error(f'rss: 无法正常打开站点 {url}')
                            return None
                        text = await resp.read()
                        return text
            else:
                import urllib.request

                def _fetch() -> bytes | None:
                    try:
                        ssl_context = ssl.create_default_context(cafile=certifi.where())
                        req = urllib.request.Request(url, headers=headers, method='GET')
                        if self._should_bypass_proxy(url):
                            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        else:
                            opener = urllib.request.build_opener()
                        with opener.open(req, timeout=30, context=ssl_context) as resp:
                            return resp.read()
                    except Exception:
                        return None
                text = await asyncio.to_thread(_fetch)
                if text is None:
                    self.logger.error(f'rss: 请求站点 {url} 失败')
                    return None
                return text
        except asyncio.TimeoutError:
            self.logger.error(f'rss: 请求站点 {url} 超时')
            return None
        except Exception as e:
            self.logger.error(f'rss: 请求站点 {url} 发生未知错误: {str(e)}')
            return None

    async def cron_task_callback(self, url: str, user: str):
        """定时任务回调"""
        if url not in self.data_handler.data:
            return
        if user not in self.data_handler.data[url]['subscribers']:
            return
        self.logger.info(f'RSS 定时任务触发: {url} - {user}')
        last_update = self.data_handler.data[url]['subscribers'][user]['last_update']
        latest_link = self.data_handler.data[url]['subscribers'][user]['latest_link']
        max_items_per_poll = self.max_items_per_poll
        rss_items = await self.poll_rss(url, num=max_items_per_poll, after_timestamp=last_update, after_link=latest_link)
        max_ts = last_update
        platform_name, message_type, session_id = user.split(':')
        if platform_name == 'aiocqhttp' and self.is_compose:
            nodes = []
            for item in rss_items:
                comps = await self._get_chain_components(item, user)
                node = Comp.Node(uin=0, name='Astrbot', content=comps)
                nodes.append(node)
                self.data_handler.data[url]['subscribers'][user]['last_update'] = int(time.time())
                max_ts = max(max_ts, item.pubDate_timestamp)
            if len(nodes) > 0:
                msc = MessageChain(chain=nodes, use_t2i_=self.t2i)
                await self.context.send_message(user, msc)
                for item in rss_items:
                    await self._send_parser_followup(item.link, user)
        else:
            for item in rss_items:
                comps = await self._get_chain_components(item, user)
                msc = MessageChain(chain=comps, use_t2i_=self.t2i)
                await self.context.send_message(user, msc)
                await self._send_parser_followup(item.link, user)
                self.data_handler.data[url]['subscribers'][user]['last_update'] = int(time.time())
                max_ts = max(max_ts, item.pubDate_timestamp)
        if rss_items:
            self.data_handler.data[url]['subscribers'][user]['last_update'] = max_ts
            self.data_handler.data[url]['subscribers'][user]['latest_link'] = rss_items[0].link
            self.data_handler.save_data()
            self.logger.info(f'RSS 定时任务 {url} 推送成功 - {user}')
        else:
            self.logger.info(f'RSS 定时任务 {url} 无消息更新 - {user}')

    async def poll_rss(self, url: str, num: int=-1, after_timestamp: int=0, after_link: str='') -> list[RSSItem]:
        """从站点拉取RSS信息"""
        text = await self.parse_channel_info(url)
        if text is None:
            self.logger.error(f'rss: 无法解析站点 {url} 的RSS信息')
            return []
        root = etree.fromstring(text)
        items = root.xpath('//item')
        cnt = 0
        rss_items = []
        for item in items:
            try:
                chan_title = self.data_handler.data[url]['info']['title'] if url in self.data_handler.data else '未知频道'
                title = item.xpath('title')[0].text
                if len(title) > self.title_max_length:
                    title = title[:self.title_max_length] + '...'
                link = item.xpath('link')[0].text
                if not re.match('^https?://', link):
                    link = self.data_handler.get_root_url(url) + link
                description = item.xpath('description')[0].text
                pic_url_list = self.data_handler.strip_html_pic(description)
                description = self.data_handler.strip_html(description)
                if len(description) > self.description_max_length:
                    description = description[:self.description_max_length] + '...'
                if item.xpath('pubDate'):
                    pub_date = item.xpath('pubDate')[0].text
                    pub_date_parsed = time.strptime(pub_date.replace('GMT', '+0000'), '%a, %d %b %Y %H:%M:%S %z')
                    pub_date_timestamp = int(time.mktime(pub_date_parsed))
                    if pub_date_timestamp > after_timestamp:
                        rss_items.append(RSSItem(chan_title, title, link, description, pub_date, pub_date_timestamp, pic_url_list))
                        cnt += 1
                        if num != -1 and cnt >= num:
                            break
                    else:
                        break
                elif link != after_link:
                    rss_items.append(RSSItem(chan_title, title, link, description, '', 0, pic_url_list))
                    cnt += 1
                    if num != -1 and cnt >= num:
                        break
                else:
                    break
            except Exception as e:
                self.logger.error(f'rss: 解析Rss条目 {url} 失败: {str(e)}')
                break
        return rss_items

    def parse_rss_url(self, url: str) -> str:
        """解析RSS URL，确保以http或https开头"""
        if not re.match('^https?://', url):
            if not url.startswith('/'):
                url = '/' + url
            url = 'https://' + url
        return url

    def _get_group_subscriptions_conf(self) -> list[dict]:
        items = self.config.get('group_subscriptions') or []
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            group_id = str(it.get('group_id') or it.get('group') or '').strip()
            url = str(it.get('url') or it.get('space_url') or it.get('idx_or_url') or '').strip()
            if not group_id or not url:
                continue
            platform_id = str(it.get('platform_id') or it.get('platform') or 'default').strip() or 'default'
            out.append({'group_id': group_id, 'url': url, 'platform_id': platform_id})
        return out

    async def _apply_group_subscriptions_config_async(self):
        items = self._get_group_subscriptions_conf()
        if not items:
            return
        changed = False
        for it in items:
            group_id = it['group_id']
            url_raw = it['url']
            platform_id = it['platform_id']
            session = self._build_group_session(platform_id, group_id)
            cron_expr = '0/30 * * * *'
            url, err = self._construct_subscription_url(idx_or_url=url_raw, route='', session=session)
            if err or not url:
                self.logger.warning(f'rss: 跳过订阅（{group_id}）：{err or 'URL 无效'}')
                continue
            info = self.data_handler.data.get(url) or {}
            subs = info.get('subscribers') or {}
            if session in subs:
                continue
            ok, err = await self._add_url_for_session_no_event(url=url, cron_expr=cron_expr, session=session)
            if not ok:
                self.logger.warning(f'rss: 订阅失败（{group_id}）：{err}')
                continue
            changed = True
        if changed:
            self._fresh_asyncIOScheduler()

    def _fresh_asyncIOScheduler(self):
        """刷新定时任务"""
        self.logger.info('刷新定时任务')
        self.scheduler.remove_all_jobs()
        for url, info in self.data_handler.data.items():
            if url == 'rsshub_endpoints' or url == 'settings':
                continue
            for user, sub_info in info['subscribers'].items():
                self.scheduler.add_job(self.cron_task_callback, 'cron', **self.parse_cron_expr(sub_info['cron_expr']), second=str(self._job_second_offset(url, user)), args=[url, user])

    async def _add_url(self, url: str, cron_expr: str, message: AstrMessageEvent):
        return await self._add_url_for_session(url=url, cron_expr=cron_expr, session=message.unified_msg_origin, reply_event=message)

    async def _add_url_for_session(self, url: str, cron_expr: str, session: str, reply_event: AstrMessageEvent):
        user = session
        if url in self.data_handler.data:
            latest_item = await self.poll_rss(url)
            if not latest_item:
                return reply_event.plain_result('订阅失败：RSS 源没有返回可用条目，请确认 RSSHub 正常运行且路由可访问。')
            self.data_handler.data[url]['subscribers'][user] = {'cron_expr': cron_expr, 'last_update': latest_item[0].pubDate_timestamp, 'latest_link': latest_item[0].link}
        else:
            try:
                text = await self.parse_channel_info(url)
                title, desc = self.data_handler.parse_channel_text_info(text)
                latest_item = await self.poll_rss(url)
            except Exception as e:
                return reply_event.plain_result(f'解析频道信息失败: {str(e)}')
            if not latest_item:
                return reply_event.plain_result('订阅失败：RSS 源没有返回可用条目，请确认 RSSHub 正常运行且路由可访问。')
            self.data_handler.data[url] = {'subscribers': {user: {'cron_expr': cron_expr, 'last_update': latest_item[0].pubDate_timestamp, 'latest_link': latest_item[0].link}}, 'info': {'title': title, 'description': desc}}
        self.data_handler.save_data()
        return self.data_handler.data[url]['info']

    async def _add_url_for_session_no_event(self, url: str, cron_expr: str, session: str) -> tuple[bool, str | None]:
        user = session
        if url in self.data_handler.data:
            latest_item = await self.poll_rss(url)
            if not latest_item:
                return (False, '订阅失败：RSS 源没有返回可用条目，请确认 RSSHub 正常运行且路由可访问。')
            self.data_handler.data[url]['subscribers'][user] = {'cron_expr': cron_expr, 'last_update': latest_item[0].pubDate_timestamp, 'latest_link': latest_item[0].link}
        else:
            try:
                text = await self.parse_channel_info(url)
                title, desc = self.data_handler.parse_channel_text_info(text)
                latest_item = await self.poll_rss(url)
            except Exception as e:
                return (False, f'解析频道信息失败: {str(e)}')
            if not latest_item:
                return (False, '订阅失败：RSS 源没有返回可用条目，请确认 RSSHub 正常运行且路由可访问。')
            self.data_handler.data[url] = {'subscribers': {user: {'cron_expr': cron_expr, 'last_update': latest_item[0].pubDate_timestamp, 'latest_link': latest_item[0].link}}, 'info': {'title': title, 'description': desc}}
        self.data_handler.save_data()
        return (True, None)

    async def _start_manage_ui(self):
        if not bool(self.config.get('manage_ui_enable', True)):
            return
        host = str(self.config.get('manage_ui_host') or '0.0.0.0')
        try:
            port = int(self.config.get('manage_ui_port') or 6190)
        except Exception:
            port = 6190
        self._manage_app = web.Application()
        self._manage_app.router.add_get('/', self._handle_manage_index)
        self._manage_app.router.add_get('/api/groups', self._handle_manage_groups)
        self._manage_app.router.add_get('/api/subs', self._handle_manage_subs)
        self._manage_app.router.add_post('/api/subs/add', self._handle_manage_add)
        self._manage_app.router.add_post('/api/subs/remove', self._handle_manage_remove)
        runner = web.AppRunner(self._manage_app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._manage_runner = runner
        self.logger.info(f'[rss] Manage UI started at http://{host}:{port}')

    async def _handle_manage_index(self, request: web.Request):
        html = '\n<!doctype html>\n<html lang="zh">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1" />\n  <title>RSS 订阅管理</title>\n  <style>\n    body { font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; background:#0f1115; color:#e6e6e6; margin:0; }\n    .wrap { max-width: 980px; margin: 24px auto; padding: 0 16px; }\n    .card { background:#161a22; border:1px solid #232a36; border-radius:12px; padding:16px; margin-bottom:16px; }\n    label { display:block; margin: 10px 0 6px; color:#b8c0cc; }\n    select,input,button { width:100%; padding:10px; border-radius:8px; border:1px solid #2a3442; background:#0f131a; color:#e6e6e6; }\n    button { cursor:pointer; background:#2563eb; border:none; margin-top:12px; }\n    button.secondary { background:#2d3748; }\n    .row { display:flex; gap:12px; }\n    .row > div { flex:1; }\n    .list { margin-top: 12px; }\n    .item { padding:10px; border:1px solid #243041; border-radius:8px; margin-bottom:8px; background:#10151d; }\n    .item small { color:#9aa4b2; }\n    .item button { width:auto; padding:6px 10px; background:#dc2626; }\n    .muted { color:#9aa4b2; }\n    .toast { margin-top:12px; padding:10px 12px; border-radius:8px; display:none; }\n    .toast.ok { background:#064e3b; color:#d1fae5; }\n    .toast.err { background:#7f1d1d; color:#fee2e2; }\n  </style>\n</head>\n<body>\n  <div class="wrap">\n    <div class="card">\n      <h2>RSS 订阅管理</h2>\n      <div class="row">\n        <div>\n          <label>选择群号</label>\n          <select id="groupSelect"></select>\n        </div>\n        <div>\n          <label>平台（自动）</label>\n          <input id="platformInput" disabled />\n        </div>\n      </div>\n      <label>订阅地址 / 空间链接 / RSSHub 索引</label>\n      <input id="urlInput" placeholder="https://space.bilibili.com/..." />\n      <label>RSSHub 路由（可选）</label>\n      <input id="routeInput" placeholder="/bilibili/user/video/xxxx" />\n      <button id="addBtn">添加订阅（默认每30分钟）</button>\n      <div class="muted" style="margin-top:8px;">只需要群号即可，轮询时间固定为 30 分钟。</div>\n      <div id="toast" class="toast"></div>\n    </div>\n\n    <div class="card">\n      <h3>当前群的订阅列表</h3>\n      <div id="subsList" class="list"></div>\n    </div>\n  </div>\n\n<script>\nconst groupSelect = document.getElementById(\'groupSelect\');\nconst platformInput = document.getElementById(\'platformInput\');\nconst urlInput = document.getElementById(\'urlInput\');\nconst routeInput = document.getElementById(\'routeInput\');\nconst subsList = document.getElementById(\'subsList\');\nconst addBtn = document.getElementById(\'addBtn\');\nconst toast = document.getElementById(\'toast\');\n\nlet groups = [];\n\nfunction showToast(text, ok) {\n  toast.textContent = text;\n  toast.className = \'toast \' + (ok ? \'ok\' : \'err\');\n  toast.style.display = \'block\';\n  setTimeout(() => { toast.style.display = \'none\'; }, 2500);\n}\n\nasync function loadGroups() {\n  const resp = await fetch(\'/api/groups\');\n  const data = await resp.json();\n  groups = data.data || [];\n  groupSelect.innerHTML = \'\';\n  for (const g of groups) {\n    const opt = document.createElement(\'option\');\n    opt.value = g.group_id;\n    opt.textContent = g.group_id;\n    opt.dataset.platform = g.platform_id;\n    groupSelect.appendChild(opt);\n  }\n  if (groups.length > 0) {\n    platformInput.value = groups[0].platform_id;\n    await loadSubs();\n  }\n}\n\nasync function loadSubs() {\n  const group_id = groupSelect.value;\n  const platform_id = platformInput.value || \'default\';\n  const resp = await fetch(`/api/subs?group_id=${encodeURIComponent(group_id)}&platform_id=${encodeURIComponent(platform_id)}`);\n  const data = await resp.json();\n  const subs = data.data || [];\n  subsList.innerHTML = \'\';\n  if (subs.length === 0) {\n    subsList.innerHTML = \'<div class="muted">暂无订阅</div>\';\n    return;\n  }\n  for (const s of subs) {\n    const div = document.createElement(\'div\');\n    div.className = \'item\';\n    div.innerHTML = `<div>${s.url}</div><small>${s.title || \'\'}</small>`;\n    const btn = document.createElement(\'button\');\n    btn.textContent = \'删除\';\n    btn.onclick = async () => {\n      await fetch(\'/api/subs/remove\', { method:\'POST\', headers:{\'Content-Type\':\'application/json\'}, body: JSON.stringify({group_id, platform_id, url: s.url}) });\n      await loadSubs();\n    };\n    div.appendChild(btn);\n    subsList.appendChild(div);\n  }\n}\n\ngroupSelect.addEventListener(\'change\', async (e) => {\n  const opt = groupSelect.selectedOptions[0];\n  platformInput.value = opt ? opt.dataset.platform : \'default\';\n  await loadSubs();\n});\n\naddBtn.addEventListener(\'click\', async () => {\n  const group_id = groupSelect.value;\n  const platform_id = platformInput.value || \'default\';\n  const url = urlInput.value.trim();\n  const route = routeInput.value.trim();\n  if (!group_id || !url) {\n    showToast(\'请填写群号与订阅地址\', false);\n    return;\n  }\n  const resp = await fetch(\'/api/subs/add\', { method:\'POST\', headers:{\'Content-Type\':\'application/json\'}, body: JSON.stringify({group_id, platform_id, url, route}) });\n  const data = await resp.json().catch(() => ({}));\n  if (data.status !== \'ok\') {\n    showToast(data.message || \'添加失败\', false);\n    return;\n  }\n  if (data.url) {\n    showToast(\'添加成功：\' + data.url, true);\n  } else {\n    showToast(\'添加成功\', true);\n  }\n  urlInput.value = \'\';\n  routeInput.value = \'\';\n  await loadSubs();\n});\n\nloadGroups();\n</script>\n</body>\n</html>\n        '
        return web.Response(text=html, content_type='text/html')

    async def _handle_manage_groups(self, request: web.Request):
        conv_mgr = self.context.conversation_manager
        groups: dict[tuple[str, str], dict] = {}
        try:
            convs, _cnt = await conv_mgr.get_filtered_conversations(page=1, page_size=2000, platform_ids=None, search_query='')
        except Exception:
            convs = []
        for conv in convs:
            session_id = getattr(conv, 'user_id', '') or ''
            parts = session_id.split(':')
            if len(parts) >= 3 and parts[1] == 'GroupMessage':
                platform_id = parts[0]
                group_id = parts[2]
                groups[platform_id, group_id] = {'platform_id': platform_id, 'group_id': group_id}
        data = [v for _, v in sorted(groups.items(), key=lambda kv: kv[0][1])]
        return web.json_response({'status': 'ok', 'data': data})

    async def _handle_manage_subs(self, request: web.Request):
        group_id = (request.query.get('group_id') or '').strip()
        platform_id = (request.query.get('platform_id') or 'default').strip() or 'default'
        if not group_id:
            return web.json_response({'status': 'ok', 'data': []})
        session = self._build_group_session(platform_id, group_id)
        out = []
        for url, info in self.data_handler.data.items():
            if url in {'rsshub_endpoints', 'settings'}:
                continue
            subs = info.get('subscribers') or {}
            if session not in subs:
                continue
            out.append({'url': url, 'title': (info.get('info') or {}).get('title'), 'cron_expr': subs.get(session, {}).get('cron_expr')})
        return web.json_response({'status': 'ok', 'data': out})

    async def _handle_manage_add(self, request: web.Request):
        try:
            payload = await request.json()
            group_id = str(payload.get('group_id') or '').strip()
            platform_id = str(payload.get('platform_id') or 'default').strip() or 'default'
            url_raw = str(payload.get('url') or '').strip()
            route = str(payload.get('route') or '').strip()
            if not group_id or not url_raw:
                return web.json_response({'status': 'error', 'message': '参数不完整'})
            session = self._build_group_session(platform_id, group_id)
            url, err = self._construct_subscription_url(idx_or_url=url_raw, route=route, session=session)
            if err or not url:
                return web.json_response({'status': 'error', 'message': err or 'URL 无效'})
            ok, err = await self._add_url_for_session_no_event(url=url, cron_expr='0/30 * * * *', session=session)
            if not ok:
                return web.json_response({'status': 'error', 'message': err or '订阅失败'})
            self._fresh_asyncIOScheduler()
            return web.json_response({'status': 'ok', 'url': url})
        except Exception as e:
            self.logger.exception(f'[rss] manage add failed: {e}')
            return web.json_response({'status': 'error', 'message': f'内部错误: {type(e).__name__}'})

    async def _handle_manage_remove(self, request: web.Request):
        payload = await request.json()
        group_id = str(payload.get('group_id') or '').strip()
        platform_id = str(payload.get('platform_id') or 'default').strip() or 'default'
        url = str(payload.get('url') or '').strip()
        if not group_id or not url:
            return web.json_response({'status': 'error', 'message': '参数不完整'})
        session = self._build_group_session(platform_id, group_id)
        if url not in self.data_handler.data:
            return web.json_response({'status': 'error', 'message': '订阅不存在'})
        subs = self.data_handler.data[url].get('subscribers') or {}
        if session not in subs:
            return web.json_response({'status': 'error', 'message': '订阅不存在'})
        subs.pop(session, None)
        if not subs:
            self.data_handler.data.pop(url, None)
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        return web.json_response({'status': 'ok'})

    async def _get_chain_components(self, item: RSSItem, session: str | None=None):
        """组装消息链"""
        comps = []
        comps.append(Comp.Plain(f'频道 {item.chan_title} 最新 Feed\n---\n标题: {item.title}\n---\n'))
        if not self.is_hide_url:
            comps.append(Comp.Plain(f'链接: {item.link}\n---\n'))
        comps.append(Comp.Plain(item.description + '\n---\n'))
        if self.is_read_pic and item.pic_urls:
            temp_max_pic_item = len(item.pic_urls) if self.max_pic_item == -1 else self.max_pic_item
            for pic_url in item.pic_urls[:temp_max_pic_item]:
                base64str = await self.pic_handler.modify_corner_pixel_to_base64(pic_url)
                if base64str is None:
                    comps.append(Comp.Plain('图片链接读取失败\n'))
                    continue
                else:
                    comps.append(Comp.Image.fromBase64(base64str))
        return comps

    def _is_url_or_ip(self, text: str) -> bool:
        """
        判断一个字符串是否为网址（http/https 开头）或 IP 地址。
        """
        url_pattern = '^(?:http|https)://.+$'
        ip_pattern = '^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
        return bool(re.match(url_pattern, text) or re.match(ip_pattern, text))

    def _extract_bilibili_uid(self, url: str) -> str | None:
        matched = re.search('https?://space\\.bilibili\\.com/(\\d+)', url)
        if not matched:
            return None
        return matched.group(1)

    def _origin_of(self, url: str) -> str:
        try:
            parts = urlsplit(url)
            scheme = parts.scheme or 'http'
            netloc = parts.netloc or parts.path
            if not netloc:
                return f'{scheme}://localhost'
            return f'{scheme}://{netloc}'
        except Exception:
            return url.rstrip('/')

    def _get_default_rsshub_endpoint(self) -> str:
        if self.default_rsshub_endpoint:
            endpoint = self._origin_of(self.default_rsshub_endpoint).rstrip('/')
            if not endpoint.startswith('http'):
                endpoint = f'http://{endpoint}'
            return endpoint
        endpoints = self.data_handler.data.get('rsshub_endpoints') or []
        for endpoint in endpoints:
            try:
                candidate = self._origin_of(endpoint).rstrip('/')
                if not candidate.startswith('http'):
                    continue
                parts = urlsplit(candidate)
                host = parts.hostname or ''
                if host.endswith('bilibili.com'):
                    continue
                return candidate
            except Exception:
                continue
        return __import__('share_runtime').resolve('{{AB_RSS_DEFAULT_RSSHUB_ENDPOINT}}')

    @filter.command_group('rss', alias={'RSS'})
    def rss(self):
        """RSS订阅插件

        可以订阅和管理多个RSS源，支持cron表达式设置更新频率

        cron 表达式格式：
        * * * * *，分别表示分钟 小时 日 月 星期，* 表示任意值，支持范围和逗号分隔。例：
        1. 0 0 * * * 表示每天 0 点触发。
        2. 0/5 * * * * 表示每 5 分钟触发。
        3. 0 9-18 * * * 表示每天 9 点到 18 点触发。
        4. 0 0 1,15 * * 表示每月 1 号和 15 号 0 点触发。
        星期的取值范围是 0-6，0 表示星期天。
        """
        pass

    @rss.group('rsshub')
    def rsshub(self, event: AstrMessageEvent):
        """RSSHub相关操作

        可以添加、查看、删除RSSHub的端点
        """
        pass

    @rsshub.command('add')
    async def rsshub_add(self, event: AstrMessageEvent, url: str):
        """添加一个RSSHub端点

        Args:
            url: RSSHub服务器地址，例如：https://rsshub.app
        """
        if not self._is_rsshub_session_allowed(event.unified_msg_origin):
            yield event.plain_result('当前会话不在 RSSHub 白名单')
            return
        url = self._origin_of(url).rstrip('/')
        if not self._is_url_or_ip(url):
            yield event.plain_result('请输入正确的URL')
            return
        elif url in self.data_handler.data['rsshub_endpoints']:
            yield event.plain_result('该RSSHub端点已存在')
            return
        else:
            self.data_handler.data['rsshub_endpoints'].append(url)
            self.data_handler.save_data()
            yield event.plain_result('添加成功')

    @rsshub.command('list')
    async def rsshub_list(self, event: AstrMessageEvent):
        """列出所有已添加的RSSHub端点"""
        if not self._is_rsshub_session_allowed(event.unified_msg_origin):
            yield event.plain_result('当前会话不在 RSSHub 白名单')
            return
        ret = '当前Bot添加的rsshub endpoint：\n'
        yield event.plain_result(ret + '\n'.join([f'{i}: {x}' for i, x in enumerate(self.data_handler.data['rsshub_endpoints'])]))

    @rsshub.command('remove')
    async def rsshub_remove(self, event: AstrMessageEvent, idx: int):
        """删除一个RSSHub端点

        Args:
            idx: 要删除的端点索引，可通过list命令查看
        """
        if not self._is_rsshub_session_allowed(event.unified_msg_origin):
            yield event.plain_result('当前会话不在 RSSHub 白名单')
            return
        if idx < 0 or idx >= len(self.data_handler.data['rsshub_endpoints']):
            yield event.plain_result('索引越界')
            return
        else:
            self.data_handler.data['rsshub_endpoints'].pop(idx)
            self.data_handler.save_data()
            yield event.plain_result('删除成功')

    @rss.command('add')
    async def add_command(self, event: AstrMessageEvent, idx_or_url: str, route: str='', minute: str='0', hour: str='*', day: str='*', month: str='*', day_of_week: str='*'):
        """通过RSSHub路由添加订阅

        Args:
            idx: RSSHub端点索引，可通过/rss rsshub list查看
            route: RSSHub路由，需以/开头
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        cron_expr = f'{minute} {hour} {day} {month} {day_of_week}'
        url, err = self._construct_subscription_url(idx_or_url=idx_or_url, route=route, session=event.unified_msg_origin)
        if err:
            yield event.plain_result(err)
            return
        assert url is not None
        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret['title']
            chan_desc = ret['description']
        self._fresh_asyncIOScheduler()
        yield event.plain_result(f'添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}')

    @rss.command('add-to')
    async def add_to_command(self, event: AstrMessageEvent, group_or_session: str, idx_or_url: str, route: str='', minute: str='0', hour: str='*', day: str='*', month: str='*', day_of_week: str='*'):
        platform_id = str(event.get_platform_id() or event.get_platform_name() or 'default')
        target = (group_or_session or '').strip()
        group_id: str | None
        if ':' in target:
            session = target
            parts = session.split(':')
            group_id = parts[-1] if len(parts) >= 3 else None
        else:
            group_id = target
            session = self._build_group_session(platform_id, group_id)
        if group_id and (not group_id.isdigit()):
            yield event.plain_result('目标必须是群号(纯数字)或 unified_msg_origin')
            return
        if not self._core_whitelist_allows(session=session, group_id=group_id):
            yield event.plain_result('目标群不在 Core 白名单中')
            return
        cron_expr = f'{minute} {hour} {day} {month} {day_of_week}'
        url, err = self._construct_subscription_url(idx_or_url=idx_or_url, route=route, session=session)
        if err:
            yield event.plain_result(err)
            return
        assert url is not None
        ret = await self._add_url_for_session(url, cron_expr, session=session, reply_event=event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        self._fresh_asyncIOScheduler()
        yield event.plain_result(f'添加成功：{session}')

    @rss.command('add-wl')
    async def add_whitelist_groups_command(self, event: AstrMessageEvent, idx_or_url: str, route: str='', minute: str='0', hour: str='*', day: str='*', month: str='*', day_of_week: str='*'):
        platform_id = str(event.get_platform_id() or event.get_platform_name() or 'default')
        group_ids = self._get_core_whitelisted_group_ids()
        if not group_ids:
            yield event.plain_result('Core 白名单为空或未启用；请先在 data/cmd_config.json 配置 platform_settings.id_whitelist')
            return
        cron_expr = f'{minute} {hour} {day} {month} {day_of_week}'
        ok: list[str] = []
        failed: list[str] = []
        for gid in group_ids:
            session = self._build_group_session(platform_id, gid)
            if not self._core_whitelist_allows(session=session, group_id=gid):
                failed.append(gid)
                continue
            url, err = self._construct_subscription_url(idx_or_url=idx_or_url, route=route, session=session)
            if err or not url:
                failed.append(gid)
                continue
            ret = await self._add_url_for_session(url, cron_expr, session=session, reply_event=event)
            if isinstance(ret, MessageEventResult):
                failed.append(gid)
                continue
            ok.append(gid)
        if ok:
            self._fresh_asyncIOScheduler()
        yield event.plain_result(f'批量添加完成\n- 成功: {len(ok)}\n- 失败: {len(failed)}\n' + (f'- 成功群: {', '.join(ok)}\n' if ok else '') + (f'- 失败群: {', '.join(failed)}' if failed else ''))

    @rss.command('bili-followings-video')
    async def bili_followings_video_command(self, event: AstrMessageEvent, uid: str, minute: str='0', hour: str='*', day: str='*', month: str='*', day_of_week: str='*'):
        if not self._is_rsshub_session_allowed(event.unified_msg_origin):
            yield event.plain_result('当前会话不在 RSSHub 白名单')
            return
        cron_expr = f'{minute} {hour} {day} {month} {day_of_week}'
        endpoint = self._get_default_rsshub_endpoint()
        url = f'{endpoint}/bilibili/followings/video/{uid}'
        url = self._with_rsshub_access_key(url, event.unified_msg_origin)
        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        chan_title = ret['title']
        chan_desc = ret['description']
        self._fresh_asyncIOScheduler()
        yield event.plain_result(f'添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}\n\n注意：该 RSSHub 路由需要在 RSSHub 侧配置对应 uid 的 BILIBILI_COOKIE_{uid}。')

    @rss.command('add-url')
    async def add_url_command(self, event: AstrMessageEvent, url: str, minute: str, hour: str, day: str, month: str, day_of_week: str):
        """直接通过Feed URL添加订阅

        Args:
            url: RSS Feed的完整URL
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        cron_expr = f'{minute} {hour} {day} {month} {day_of_week}'
        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret['title']
            chan_desc = ret['description']
        self._fresh_asyncIOScheduler()
        yield event.plain_result(f'添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}')

    @rss.command('list')
    async def list_command(self, event: AstrMessageEvent):
        """列出当前所有订阅的RSS频道"""
        user = event.unified_msg_origin
        ret = '当前订阅的频道：\n'
        subs_urls = self.data_handler.get_subs_channel_url(user)
        cnt = 0
        for url in subs_urls:
            info = self.data_handler.data[url]['info']
            ret += f'{cnt}. {info['title']} - {info['description']}\n'
            cnt += 1
        yield event.plain_result(ret)

    @rss.command('remove')
    async def remove_command(self, event: AstrMessageEvent, idx: int):
        """删除一个RSS订阅

        Args:
            idx: 要删除的订阅索引，可通过/rss list查看
        """
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result('索引越界, 请使用 /rss list 查看已经添加的订阅')
            return
        url = subs_urls[idx]
        self.data_handler.data[url]['subscribers'].pop(event.unified_msg_origin)
        self.data_handler.save_data()
        self._fresh_asyncIOScheduler()
        yield event.plain_result('删除成功')

    @rss.command('get')
    async def get_command(self, event: AstrMessageEvent, idx: int):
        """获取指定订阅的最新内容

        Args:
            idx: 要查看的订阅索引，可通过/rss list查看
        """
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result('索引越界, 请使用 /rss list 查看已经添加的订阅')
            return
        url = subs_urls[idx]
        rss_items = await self.poll_rss(url)
        if not rss_items:
            yield event.plain_result('没有新的订阅内容')
            return
        item = rss_items[0]
        platform_name, message_type, session_id = event.unified_msg_origin.split(':')
        comps = await self._get_chain_components(item, event.unified_msg_origin)
        if platform_name == 'aiocqhttp' and self.is_compose:
            node = Comp.Node(uin=0, name='Astrbot', content=comps)
            yield event.chain_result([node]).use_t2i(self.t2i)
        else:
            yield event.chain_result(comps).use_t2i(self.t2i)
        await self._send_parser_followup(item.link, event.unified_msg_origin)
