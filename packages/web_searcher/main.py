import asyncio
import random
from datetime import datetime, timezone

from astrbot.api import AstrBotConfig, llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core.provider.func_tool_manager import FunctionToolManager

from .engines import HEADERS, USER_AGENTS, SearchResult
from .engines.bing import Bing
from .engines.sogo import Sogo

try:
    from pixivpy3 import AppPixivAPI
except ImportError:
    AppPixivAPI = None


class Main(star.Star):
    TOOLS = [
        "web_search",
        "fetch_url",
        "web_search_tavily",
        "tavily_extract_web_page",
        "search_images",
    ]

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self.tavily_key_index = 0
        self.tavily_key_lock = asyncio.Lock()

        # 将 str 类型的 key 迁移至 list[str]，并保存
        cfg = self.context.get_config()
        provider_settings = cfg.get("provider_settings")
        if provider_settings:
            tavily_key = provider_settings.get("websearch_tavily_key")
            if isinstance(tavily_key, str):
                logger.info(
                    "检测到旧版 websearch_tavily_key (字符串格式)，自动迁移为列表格式并保存。",
                )
                if tavily_key:
                    provider_settings["websearch_tavily_key"] = [tavily_key]
                else:
                    provider_settings["websearch_tavily_key"] = []
                cfg.save_config()

        self.bing_search = Bing()
        self.sogo_search = Sogo()
        self.baidu_initialized = False

    async def _tidy_text(self, text: str) -> str:
        """清理文本，去除空格、换行符等"""
        return text.strip().replace("\n", " ").replace("\r", " ").replace("  ", " ")

    async def _get_from_url(self, url: str) -> str:
        """获取网页内容"""
        header = HEADERS
        header.update({"User-Agent": random.choice(USER_AGENTS)})
        try:
            try:
                import aiohttp  # lazy
            except Exception:
                aiohttp = None

            if aiohttp is not None:
                async with aiohttp.ClientSession(trust_env=True) as session:
                    async with session.get(url, headers=header, timeout=6) as response:
                        html = await response.text(encoding="utf-8")
            else:
                import urllib.request

                def _fetch() -> bytes:
                    req = urllib.request.Request(url, headers=header, method="GET")
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        return resp.read()

                html = (await asyncio.to_thread(_fetch)).decode(
                    "utf-8", errors="ignore"
                )

            try:
                from bs4 import BeautifulSoup as _BeautifulSoup
                from readability import Document as _Document
            except Exception:
                _Document = None
                _BeautifulSoup = None

            if _Document is not None and _BeautifulSoup is not None:
                doc = _Document(html)
                ret = doc.summary(html_partial=True)
                soup = _BeautifulSoup(ret, "html.parser")

                # Extract images
                images = []
                for img in soup.find_all("img", src=True):
                    src = img["src"]
                    if src.startswith("http"):
                        images.append(src)

                ret = await self._tidy_text(soup.get_text())

                if images:
                    ret += "\n\nDetected Images:\n" + "\n".join(images[:5])

                return ret
            else:
                import re as _re

                # Extract images using regex before stripping tags
                images = _re.findall(
                    r'(https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|gif|webp))', html
                )

                text = _re.sub(r"<[^>]+>", " ", html)
                ret = await self._tidy_text(text)

                if images:
                    ret += "\n\nDetected Images:\n" + "\n".join(images[:5])

                return ret
        except BaseException:
            return ""

    async def _process_search_result(
        self,
        result: SearchResult,
        idx: int,
        websearch_link: bool,
    ) -> str:
        """处理单个搜索结果"""
        logger.info(f"web_searcher - scraping web: {result.title} - {result.url}")
        try:
            site_result = await self._get_from_url(result.url)
        except BaseException:
            site_result = ""
        site_result = (
            f"{site_result[:700]}..." if len(site_result) > 700 else site_result
        )

        header = f"{idx}. {result.title} "

        if websearch_link and result.url:
            header += result.url

        return f"{header}\n{result.snippet}\n{site_result}\n\n"

    async def _web_search_default(
        self,
        query,
        num_results: int = 5,
    ) -> list[SearchResult]:
        results = []
        try:
            results = await self.bing_search.search(query, num_results)
        except Exception as e:
            logger.error(f"bing search error: {e}, try the next one...")
        if len(results) == 0:
            logger.debug("search bing failed")
            try:
                results = await self.sogo_search.search(query, num_results)
            except Exception as e:
                logger.error(f"sogo search error: {e}")
        if len(results) == 0:
            logger.debug("search sogo failed")
            return []

        return results

    async def _get_tavily_key(self, cfg: AstrBotConfig) -> str:
        """并发安全的从列表中获取并轮换Tavily API密钥。"""
        tavily_keys = cfg.get("provider_settings", {}).get("websearch_tavily_key", [])
        if not tavily_keys:
            raise ValueError("错误：Tavily API密钥未在AstrBot中配置。")

        async with self.tavily_key_lock:
            key = tavily_keys[self.tavily_key_index]
            self.tavily_key_index = (self.tavily_key_index + 1) % len(tavily_keys)
            return key

    async def _web_search_tavily(
        self,
        cfg: AstrBotConfig,
        payload: dict,
    ) -> list[SearchResult]:
        """使用 Tavily 搜索引擎进行搜索"""
        tavily_key = await self._get_tavily_key(cfg)
        url = "https://api.tavily.com/search"
        header = {
            "Authorization": f"Bearer {tavily_key}",
            "Content-Type": "application/json",
        }
        data = {}
        try:
            import aiohttp  # lazy
        except Exception:
            aiohttp = None

        if aiohttp is not None:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=header,
                    timeout=6,
                ) as response:
                    if response.status != 200:
                        reason = await response.text()
                        raise Exception(
                            f"Tavily web search failed: {reason}, status: {response.status}",
                        )
                    data = await response.json()
        else:
            import json as _json
            import urllib.request

            def _post() -> dict:
                req = urllib.request.Request(
                    url,
                    data=_json.dumps(payload).encode("utf-8"),
                    headers=header,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    if resp.status != 200:
                        reason = resp.read().decode("utf-8", errors="ignore")
                        raise Exception(
                            f"Tavily web search failed: {reason}, status: {resp.status}",
                        )
                    return _json.loads(resp.read().decode("utf-8"))

            data = await asyncio.to_thread(_post)

        results = []
        for item in data.get("results", []):
            result = SearchResult(
                title=item.get("title"),
                url=item.get("url"),
                snippet=item.get("content"),
            )
            results.append(result)
        return results

    async def _extract_tavily(self, cfg: AstrBotConfig, payload: dict) -> list[dict]:
        """使用 Tavily 提取网页内容"""
        tavily_key = await self._get_tavily_key(cfg)
        url = "https://api.tavily.com/extract"
        header = {
            "Authorization": f"Bearer {tavily_key}",
            "Content-Type": "application/json",
        }
        try:
            import aiohttp  # lazy
        except Exception:
            aiohttp = None

        if aiohttp is not None:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=header,
                    timeout=6,
                ) as response:
                    if response.status != 200:
                        reason = await response.text()
                        raise Exception(
                            f"Tavily web search failed: {reason}, status: {response.status}",
                        )
                    data = await response.json()
        else:
            import json as _json
            import urllib.request

            def _post() -> dict:
                req = urllib.request.Request(
                    url,
                    data=_json.dumps(payload).encode("utf-8"),
                    headers=header | {"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=6) as resp:
                    return _json.loads(resp.read().decode("utf-8"))

            data = await asyncio.to_thread(_post)

        results: list[dict] = data.get("results", [])
        if not results:
            raise ValueError(
                "Error: Tavily web searcher does not return any results.",
            )
        return results

    @filter.command("websearch")
    async def websearch(self, event: AstrMessageEvent, oper: str | None = None):
        event.set_result(
            MessageEventResult().message(
                "此指令已经被废弃，请在 WebUI 中开启或关闭网页搜索功能。",
            ),
        )

    @llm_tool(name="web_search")
    async def search_from_search_engine(
        self,
        event: AstrMessageEvent,
        query: str,
        max_results: int = 5,
    ) -> str:
        """搜索网络以回答用户的问题。当用户需要搜索网络以获取即时性的信息时调用此工具。

        Args:
            query(string): 和用户的问题最相关的搜索关键词，用于在 Google 上搜索。
            max_results(number): 返回的最大搜索结果数量，默认为 5。

        """
        logger.info(f"web_searcher - search_from_search_engine: {query}")
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        websearch_link = cfg["provider_settings"].get("web_search_link", False)

        results = await self._web_search_default(query, max_results)
        if not results:
            return "Error: web searcher does not return any results."

        tasks = []
        for idx, result in enumerate(results, 1):
            task = self._process_search_result(result, idx, websearch_link)
            tasks.append(task)
        processed_results = await asyncio.gather(*tasks, return_exceptions=True)
        ret = ""
        for processed_result in processed_results:
            if isinstance(processed_result, BaseException):
                logger.error(f"Error processing search result: {processed_result}")
                continue
            ret += processed_result

        if websearch_link:
            ret += "\n\n针对问题，请根据上面的结果分点总结，并且在结尾处附上对应内容的参考链接（如有）。"

        return ret

    async def ensure_baidu_ai_search_mcp(self, umo: str | None = None):
        if self.baidu_initialized:
            return
        cfg = self.context.get_config(umo=umo)
        key = cfg.get("provider_settings", {}).get(
            "websearch_baidu_app_builder_key",
            "",
        )
        if not key:
            raise ValueError(
                "Error: Baidu AI Search API key is not configured in AstrBot.",
            )
        func_tool_mgr = self.context.get_llm_tool_manager()
        await func_tool_mgr.enable_mcp_server(
            "baidu_ai_search",
            config={
                "transport": "sse",
                "url": f"http://appbuilder.baidu.com/v2/ai_search/mcp/sse?api_key={key}",
                "headers": {},
                "timeout": 30,
            },
        )
        self.baidu_initialized = True
        logger.info("Successfully initialized Baidu AI Search MCP server.")

    @llm_tool(name="fetch_url")
    async def fetch_website_content(self, event: AstrMessageEvent, url: str) -> str:
        """Fetch the content of a website with the given web url

        Args:
            url(string): The url of the website to fetch content from

        """
        resp = await self._get_from_url(url)
        return resp

    @llm_tool("web_search_tavily")
    async def search_from_tavily(
        self,
        event: AstrMessageEvent,
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        topic: str = "general",
        days: int = 3,
        time_range: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """A web search tool that uses Tavily to search the web for relevant content.
        Ideal for gathering current information, news, and detailed web content analysis.

        Args:
            query(string): Required. Search query.
            max_results(number): Optional. The maximum number of results to return. Default is 5. Range is 5-20.
            search_depth(string): Optional. The depth of the search, must be one of 'basic', 'advanced'. Default is "basic".
            topic(string): Optional. The topic of the search, must be one of 'general', 'news'. Default is "general".
            days(number): Optional. The number of days back from the current date to include in the search results. Please note that this feature is only available when using the 'news' search topic.
            time_range(string): Optional. The time range back from the current date to include in the search results. This feature is available for both 'general' and 'news' search topics. Must be one of 'day', 'week', 'month', 'year'.
            start_date(string): Optional. The start date for the search results in the format 'YYYY-MM-DD'.
            end_date(string): Optional. The end date for the search results in the format 'YYYY-MM-DD'.

        """
        logger.info(f"web_searcher - search_from_tavily: {query}")
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        websearch_link = cfg["provider_settings"].get("web_search_link", False)
        if not cfg.get("provider_settings", {}).get("websearch_tavily_key", []):
            raise ValueError("Error: Tavily API key is not configured in AstrBot.")

        # build payload
        payload = {
            "query": query,
            "max_results": max_results,
        }
        if search_depth not in ["basic", "advanced"]:
            search_depth = "basic"
        payload["search_depth"] = search_depth

        if topic not in ["general", "news"]:
            topic = "general"
        payload["topic"] = topic

        if topic == "news":
            payload["days"] = days

        if time_range in ["day", "week", "month", "year"]:
            payload["time_range"] = time_range
        if start_date:
            payload["start_date"] = start_date
        if end_date:
            payload["end_date"] = end_date

        results = await self._web_search_tavily(cfg, payload)
        if not results:
            return "Error: Tavily web searcher does not return any results."

        # Filter and Sort results based on domain reliability
        whitelist = [
            "wikimedia.org",
            "unsplash.com",
            "imgur.com",
            "redd.it",
            "googleusercontent.com",
            "steamstatic.com",
            "pixiv.net",
            "i.pximg.net",
            "twitter.com",
            "x.com",
            "pbs.twimg.com",
        ]
        blacklist = [
            "loli.net",
            "sm.ms",
            "zhihu.com",
            "weibo.com",
            "bilibili.com",
            "xiaohongshu.com",
            "qq.com",
            "163.com",
            "tumblr.com",
            "pinterest.com",
        ]

        filtered_results = []
        blacklisted_results = []

        for result in results:
            url_str = str(result.url).lower()
            if any(b in url_str for b in blacklist):
                blacklisted_results.append(result)
            else:
                filtered_results.append(result)

        # Sort filtered results: Whitelisted domains first
        filtered_results.sort(
            key=lambda r: any(w in str(r.url).lower() for w in whitelist), reverse=True
        )

        # Use filtered results if available, otherwise fallback to blacklisted but warn
        final_results = filtered_results if filtered_results else blacklisted_results

        ret_ls = []
        for idx, result in enumerate(final_results, 1):
            ret_ls.append(
                f"[{idx}] {result.title} - {result.snippet} (URL: {result.url})"
            )
        ret = "\n".join(ret_ls)

        # Add instruction
        system_note = "System: 基于这些结果回答，别瞎编。"
        if not filtered_results and blacklisted_results:
            system_note += (
                "\nWarning: All results are from potentially unstable sources."
            )

        ret = f"Here are the search results from Tavily:\n{ret}\n\n{system_note}"

        if websearch_link:
            ret += "\n\n针对问题，请根据上面的结果分点总结，并且在结尾处附上对应内容的参考链接（如有）。"
        return ret

    @llm_tool("tavily_extract_web_page")
    async def tavily_extract_web_page(
        self,
        event: AstrMessageEvent,
        url: str = "",
        extract_depth: str = "basic",
    ) -> str:
        """Extract the content of a web page using Tavily.

        Args:
            url(string): Required. An URl to extract content from.
            extract_depth(string): Optional. The depth of the extraction, must be one of 'basic', 'advanced'. Default is "basic".

        """
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        if not cfg.get("provider_settings", {}).get("websearch_tavily_key", []):
            raise ValueError("Error: Tavily API key is not configured in AstrBot.")

        if not url:
            raise ValueError("Error: url must be a non-empty string.")
        if extract_depth not in ["basic", "advanced"]:
            extract_depth = "basic"
        payload = {
            "urls": [url],
            "extract_depth": extract_depth,
        }
        results = await self._extract_tavily(cfg, payload)
        ret_ls = []
        for result in results:
            ret_ls.append(f"URL: {result.get('url', 'No URL')}")
            ret_ls.append(f"Content: {result.get('raw_content', 'No content')}")
        ret = "\n".join(ret_ls)
        if not ret:
            return "Error: Tavily web searcher does not return any results."
        return ret

    async def _search_pixiv_api(
        self, cfg: dict, query: str, max_results: int
    ) -> list[str]:
        if not AppPixivAPI:
            return []

        refresh_token = cfg.get("provider_settings", {}).get("pixiv_refresh_token")
        if not refresh_token:
            return []

        # R-18 筛选设置（来自全局或 provider_settings，默认不允许）
        allow_r18 = bool(
            cfg.get("pixiv_allow_r18", False)
            or cfg.get("provider_settings", {}).get("pixiv_allow_r18", False)
        )
        allow_r18g = bool(
            cfg.get("pixiv_allow_r18g", False)
            or cfg.get("provider_settings", {}).get("pixiv_allow_r18g", False)
        )

        # 轻度清洗查询词，去掉常见噪声词（不改变用户语义）
        noise = {"イラスト", "插画", "fanart", "アート", "艺术", "art"}
        try:
            parts = [
                p for p in [s.strip() for s in query.split()] if p and p not in noise
            ]
        except Exception:
            parts = [query.strip()] if query else []
        q_clean = " ".join(parts) if parts else (query or "")

        # 构造多策略检索：精确标签 / 标题与说明 / 标签部分匹配（不限制时间）
        strategies = []
        # 优先精确标签（若只有一个核心词）
        if len(parts) == 1:
            strategies.append((parts[0], "exact_match_for_tags"))
        # 标题与说明匹配
        strategies.append((q_clean, "title_and_caption"))
        # 标签部分匹配
        strategies.append((q_clean, "partial_match_for_tags"))
        # 若多词，尝试单词回退
        if len(parts) > 1:
            for p in parts:
                strategies.append((p, "partial_match_for_tags"))

        try:
            api = AppPixivAPI()
            api.auth(refresh_token=refresh_token)

            # 近期优先：默认优先返回最近 90 天的作品，不足再补老作品
            recent_days = 90
            try:
                recent_days = int(
                    cfg.get("provider_settings", {}).get(
                        "pixiv_recent_days", cfg.get("pixiv_recent_days", recent_days)
                    )
                )
            except Exception:
                recent_days = 90

            recent_items: list[tuple[str, str, str]] = []  # (title, user, url)
            older_items: list[tuple[str, str, str]] = []
            seen = set()

            for q, target in strategies:
                if len(recent_items) >= max_results:
                    break
                try:
                    jr = api.search_illust(q, search_target=target, sort="date_desc")
                except Exception:
                    continue
                illusts = jr.get("illusts", []) if isinstance(jr, dict) else []

                # 分页拉取，直到近期结果已达上限或没有下一页
                next_url = jr.get("next_url") if isinstance(jr, dict) else None
                while True:
                    for illust in illusts:
                        if len(recent_items) >= max_results:
                            break
                        if illust.get("type") != "illust":
                            continue
                        pid = illust.get("id")
                        if not pid or pid in seen:
                            continue
                        # R-18 / R-18G 过滤
                        xr = int(illust.get("x_restrict", 0) or 0)
                        if (xr == 1 and not allow_r18) or (xr == 2 and not allow_r18g):
                            continue
                        seen.add(pid)
                        title = illust.get("title") or ""
                        user = illust.get("user", {}).get("name") or ""
                        url = f"https://www.pixiv.net/artworks/{pid}"

                        # 计算作品时间，优先收集近期作品
                        is_recent = False
                        try:
                            cd = illust.get("create_date") or illust.get(
                                "create_datetime"
                            )
                            if isinstance(cd, str) and cd:
                                # 兼容 Z 与时区偏移
                                if cd.endswith("Z"):
                                    cd = cd.replace("Z", "+00:00")
                                dt = datetime.fromisoformat(cd)
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                age_days = (
                                    datetime.now(timezone.utc)
                                    - dt.astimezone(timezone.utc)
                                ).days
                                is_recent = age_days <= recent_days
                        except Exception:
                            is_recent = False

                        if is_recent:
                            recent_items.append((title, user, url))
                        else:
                            older_items.append((title, user, url))

                    if len(recent_items) >= max_results or not next_url:
                        break
                    try:
                        params = api.parse_qs(next_url)
                        jr = api.search_illust(**params)
                        illusts = jr.get("illusts", []) if isinstance(jr, dict) else []
                        next_url = jr.get("next_url") if isinstance(jr, dict) else None
                    except Exception:
                        break

            # 组装输出：先近期，后补老作品
            ret: list[str] = []
            idx = 1
            for title, user, url in recent_items[:max_results]:
                ret.append(f"[{idx}] {title} - Author: {user} (URL: {url})")
                idx += 1
            if len(ret) < max_results:
                for title, user, url in older_items:
                    if len(ret) >= max_results:
                        break
                    ret.append(f"[{idx}] {title} - Author: {user} (URL: {url})")
                    idx += 1

            return ret
        except Exception as e:
            logger.error(f"Pixiv API search failed: {e}")
            return []

    @llm_tool("search_images")
    async def search_images(
        self,
        event: AstrMessageEvent,
        query: str,
        platform: str = "pixiv",
        max_results: int = 10,
    ) -> str:
        """Search for images/artworks on Pixiv.
        Use this tool when the user asks for images, pictures, or artworks.

        Args:
            query(string): Required. The keywords to search for.
            platform(string): Optional. The platform to search on. Default is 'pixiv'.
            max_results(int): Optional. Max results. Default 10.
        """
        cfg = self.context.get_config(umo=event.unified_msg_origin)

        # Check Pixiv Token and Search
        pixiv_results = await self._search_pixiv_api(cfg, query, max_results)

        if not pixiv_results:
            return "No valid image/artwork pages found on Pixiv."

        ret = "\n".join(pixiv_results)
        return f"Here are the images/artworks found on Pixiv:\n{ret}\n\nSystem: Please select the best URL and output it. The system will automatically fetch the image."

    @filter.on_llm_request(priority=-10000)
    async def edit_web_search_tools(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ):
        """Get the session conversation for the given event."""
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        prov_settings = cfg.get("provider_settings", {})
        websearch_enable = prov_settings.get("web_search", False)
        provider = prov_settings.get("websearch_provider", "default")

        tool_set = req.func_tool
        if isinstance(tool_set, FunctionToolManager):
            req.func_tool = tool_set.get_full_tool_set()
            tool_set = req.func_tool

        if not tool_set:
            return

        func_tool_mgr = self.context.get_llm_tool_manager()
        search_images_tool = func_tool_mgr.get_func("search_images")
        if search_images_tool:
            tool_set.add_tool(search_images_tool)

        if not websearch_enable:
            for tool_name in [
                "web_search",
                "fetch_url",
                "web_search_tavily",
                "tavily_extract_web_page",
                "AIsearch",
            ]:
                tool_set.remove_tool(tool_name)
            return

        if provider == "default":
            web_search_t = func_tool_mgr.get_func("web_search")
            fetch_url_t = func_tool_mgr.get_func("fetch_url")
            if web_search_t:
                tool_set.add_tool(web_search_t)
            if fetch_url_t:
                tool_set.add_tool(fetch_url_t)
            tool_set.remove_tool("web_search_tavily")
            tool_set.remove_tool("tavily_extract_web_page")
            tool_set.remove_tool("AIsearch")
        elif provider == "tavily":
            web_search_tavily = func_tool_mgr.get_func("web_search_tavily")
            tavily_extract_web_page = func_tool_mgr.get_func("tavily_extract_web_page")
            if web_search_tavily:
                tool_set.add_tool(web_search_tavily)
            if tavily_extract_web_page:
                tool_set.add_tool(tavily_extract_web_page)
            tool_set.remove_tool("web_search")
            tool_set.remove_tool("fetch_url")
            tool_set.remove_tool("AIsearch")
        elif provider == "baidu_ai_search":
            try:
                await self.ensure_baidu_ai_search_mcp(event.unified_msg_origin)
                aisearch_tool = func_tool_mgr.get_func("AIsearch")
                if not aisearch_tool:
                    raise ValueError("Cannot get Baidu AI Search MCP tool.")
                tool_set.add_tool(aisearch_tool)
                tool_set.remove_tool("web_search")
                tool_set.remove_tool("fetch_url")
                tool_set.remove_tool("web_search_tavily")
                tool_set.remove_tool("tavily_extract_web_page")
            except Exception as e:
                logger.error(f"Cannot Initialize Baidu AI Search MCP Server: {e}")
