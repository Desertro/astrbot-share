from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register("AstrBot_plugin_0721anime", "mingrixiangnai", "0721动漫搜索", "1.0", "https://github.com/mingrixiangnai/0721anime")
class DM0721SearchPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": "https://dm0721.icu/"
        }
        self.base_url = "https://dm0721.icu/p_sousuo.html?wd="

    @filter.command("查番")
    async def search_anime(self, event: AstrMessageEvent):
        """查询0721动漫信息\n用法：/查番 动漫名称"""
        args = event.message_str.split(maxsplit=1)
        if len(args) < 2:
            yield event.plain_result("请输入要查询的动漫名称，例如：/查番 我独自升级")
            return

        keyword = args[1]
        try:
            url = f"{self.base_url}{keyword}"
            html = ""
            try:
                import aiohttp  # lazy
            except Exception:
                aiohttp = None

            if aiohttp is not None:
                async with aiohttp.ClientSession(headers=self.headers, trust_env=True) as session:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status != 200:
                            raise Exception(f"HTTP {resp.status}")
                        html = await resp.text()
            else:
                import urllib.request

                def _fetch() -> str:
                    req = urllib.request.Request(url, headers=self.headers, method="GET")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return resp.read().decode("utf-8", errors="ignore")

                html = await asyncio.to_thread(_fetch)

            soup = BeautifulSoup(html, "html.parser")
            ul_tag = soup.find("ul", class_="row")
            if not ul_tag:
                yield event.plain_result(f"未找到与「{keyword}」相关的动漫")
                return

            results = []
            for li in ul_tag.find_all("li"):
                anime = {
                    "title": "",
                    "episodes": "",
                    "detail_url": ""
                }

                # 提取标题和详情链接
                name_div = li.find("div", class_="name")
                if name_div:
                    a_tag = name_div.find("a")
                    if a_tag:
                        anime["title"] = a_tag.get("title", "").strip()
                        # 处理详情链接
                        detail_url = a_tag.get("href", "")
                        if detail_url and not detail_url.startswith("http"):
                            detail_url = f"https://dm0721.icu{detail_url}"
                        anime["detail_url"] = detail_url

                    # 提取集数
                    p_tag = name_div.find("p")
                    if p_tag:
                        anime["episodes"] = p_tag.get_text(strip=True)

                if anime["title"]:
                    results.append(anime)

            if not results:
                yield event.plain_result(f"未找到与「{keyword}」相关的动漫")
                return

            # 构建合并消息
            msg = [f"🔍找到{len(results)}条结果：\n"]
            for index, anime in enumerate(results, 1):
                entry = f"{index}.\n   📺 【标题】：{anime['title']}\n"
                entry += f"   🎬 【集数】：{anime['episodes']}\n"
                if anime["detail_url"]:
                    entry += f"   🔗 【详情】：\n{anime['detail_url']}\n"
                msg.append(entry)

            # 合并为单条消息发送
            yield event.plain_result("\n".join(msg))

        except Exception as e:
            logger.error(f"搜索失败: {str(e)}", exc_info=True)
            yield event.plain_result("动漫查询服务暂时不可用，请稍后再试")

    async def terminate(self):
        """清理资源"""
        pass
