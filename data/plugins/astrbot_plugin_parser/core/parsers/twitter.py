import re
from typing import Any, ClassVar

from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag

from astrbot.core.config.astrbot_config import AstrBotConfig

from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="推特")

    def __init__(self, config: AstrBotConfig, downloader: Downloader):
        super().__init__(config, downloader)

    async def _req_xdown_api(self, url: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://xdown.app",
            "Referer": "https://xdown.app/",
            **self.headers,
        }
        data = {"q": url, "lang": "zh-cn"}

        async def _do_post(use_proxy: bool) -> dict[str, Any]:
            kwargs: dict[str, Any] = {}
            if use_proxy and self.proxy:
                kwargs["proxy"] = self.proxy

            async with self.client.post(
                "https://xdown.app/api/ajaxSearch",
                data=data,
                headers=headers,
                **kwargs,
            ) as resp:
                if resp.status >= 400:
                    raise ClientError(f"xdown API {resp.status} {resp.reason}")
                return await resp.json()

        try:
            return await _do_post(use_proxy=True)
        except ClientError as e:
            # 显式代理失败时回退一次，让 aiohttp 走 trust_env 自动代理/直连逻辑。
            if self.proxy:
                try:
                    return await _do_post(use_proxy=False)
                except ClientError:
                    pass
            raise ParseException(f"xdown 请求失败: {e}") from e
        except TimeoutError as e:
            raise ParseException("xdown 请求超时，请稍后重试") from e

    @handle("x.com", r"https?://x.com/[0-9-a-zA-Z_]{1,20}/status/([0-9]+)")
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        # 从匹配对象中获取原始URL
        url = searched.group(0)
        resp = await self._req_xdown_api(url)
        if resp.get("status") != "ok":
            msg = str(resp.get("msg") or "解析失败")
            raise ParseException(msg)

        html_content = resp.get("data")

        if html_content is None:
            msg = str(resp.get("msg") or "解析失败, 数据为空")
            raise ParseException(msg)

        return self.parse_twitter_html(html_content)

    def parse_twitter_html(self, html_content: str) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # 1. 提取标题
        title_tag = soup.find("h3")
        title = None
        if title_tag:
            title = title_tag.get_text(strip=True)

        # 2. 按单个媒体块解析，避免视频 / gif 的封面图被当作正文图片发出
        contents = []
        media_blocks = soup.find_all("div", class_="tw-video")

        for block in media_blocks:
            if not isinstance(block, Tag):
                continue

            cover_url = None
            thumb_tag = block.find("img")
            if isinstance(thumb_tag, Tag):
                cover = thumb_tag.get("src")
                if cover:
                    cover_url = str(cover)

            block_video_url = None
            block_dynamic_urls: list[str] = []
            block_image_urls: list[str] = []

            button_tags = block.find_all("a", class_=["tw-button-dl", "abutton"])
            for tag in button_tags:
                if not isinstance(tag, Tag):
                    continue
                href = tag.get("href")
                if href is None:
                    continue

                href = str(href)
                text = tag.get_text(strip=True)
                if "下载 gif" in text:
                    block_dynamic_urls.append(href)
                elif "下载 MP4" in text:
                    block_video_url = href
                elif "下载图片" in text:
                    block_image_urls.append(href)

            if block_dynamic_urls:
                contents.extend(self.create_dynamic_contents(block_dynamic_urls))
                continue

            if block_video_url:
                contents.append(self.create_video_content(block_video_url, cover_url))
                continue

            if block_image_urls:
                contents.extend(self.create_image_contents(block_image_urls))

        # 纯图片推文的 xdown 页面通常不走 tw-video，而是直接给 download-box / abutton
        if not contents:
            image_urls: list[str] = []
            for tag in soup.find_all("a", class_=["tw-button-dl", "abutton"]):
                if not isinstance(tag, Tag):
                    continue
                href = tag.get("href")
                if href is None:
                    continue
                text = tag.get_text(strip=True)
                if "下载图片" not in text:
                    continue
                image_urls.append(str(href))

            if image_urls:
                contents.extend(self.create_image_contents(image_urls))

        return self.result(
            title=title,
            author=self.create_author("无用户名"),
            contents=contents,
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):
