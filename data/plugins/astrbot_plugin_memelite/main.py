import asyncio

import astrbot.core.message.components as Comp
from astrbot import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.meme import MemeManager
from .core.param import ParamsCollector
from .utils import (
    compress_image,
    is_effectively_black_image,
    optimize_static_image,
    save_image_temp,
)


@register("astrbot_plugin_memelite", "Zhalslar", "...", "...")
class MemePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self.collector = ParamsCollector(config)
        self.manager = MemeManager(config, self.collector)
        self._meme_help_image_cache: bytes | None = None

    async def initialize(self):
        await self.manager.check_resources()

    @filter.command("meme帮助", alias={"表情帮助", "meme菜单", "meme列表"})
    async def memes_help(self, event):
        if self._meme_help_image_cache is None:
            output = await self.manager.render_meme_list_image()
            if output:
                self._meme_help_image_cache = await asyncio.to_thread(
                    optimize_static_image, output
                )

        if self._meme_help_image_cache:
            yield event.chain_result(
                [
                    Comp.Image.fromFileSystem(
                        save_image_temp(self._meme_help_image_cache, "meme_help")
                    )
                ]
            )
        else:
            yield event.plain_result("meme列表图生成失败")

    @filter.command("meme详情", alias={"表情详情", "meme信息"})
    async def meme_details_show(
        self, event: AstrMessageEvent, keyword: GreedyStr = ""
    ):
        keyword = str(keyword or "").strip()
        if not keyword:
            yield event.plain_result("未指定要查看的meme")
            return

        image = await asyncio.to_thread(self.manager.render_meme_detail_image, keyword)
        if not image:
            yield event.plain_result("未找到相关meme")
            return
        image = await asyncio.to_thread(optimize_static_image, image, 1600, 88)

        yield event.chain_result(
            [Comp.Image.fromFileSystem(save_image_temp(image, "meme_detail"))]
        )

    @filter.command("meme搜索", alias={"表情搜索", "meme查找"})
    async def meme_search(self, event: AstrMessageEvent, query: GreedyStr):
        image = await asyncio.to_thread(
            self.manager.render_meme_search_image,
            str(query or "").strip(),
            30,
        )
        image = await asyncio.to_thread(optimize_static_image, image, 1600, 88)
        yield event.chain_result(
            [Comp.Image.fromFileSystem(save_image_temp(image, "meme_search"))]
        )

    @filter.command("meme指令", alias={"meme命令", "meme用法", "表情指令"})
    async def meme_commands(self, event: AstrMessageEvent):
        image = await asyncio.to_thread(self.manager.render_meme_commands_image)
        image = await asyncio.to_thread(optimize_static_image, image, 1600, 88)
        yield event.chain_result(
            [Comp.Image.fromFileSystem(save_image_temp(image, "meme_commands"))]
        )

    @filter.command("禁用meme")
    async def add_supervisor(
        self, event: AstrMessageEvent, meme_name: str | None = None
    ):
        """禁用meme"""
        if not meme_name:
            yield event.plain_result("未指定要禁用的meme")
            return
        if not self.manager.is_meme_keyword(meme_name):
            yield event.plain_result(f"meme: {meme_name} 不存在")
            return
        if meme_name in self.conf["memes_disabled_list"]:
            yield event.plain_result(f"meme: {meme_name} 已被禁用")
            return
        self.conf["memes_disabled_list"].append(meme_name)
        self.conf.save_config()
        yield event.plain_result(f"已禁用meme: {meme_name}")
        logger.info(f"当前禁用meme: {self.conf['memes_disabled_list']}")

    @filter.command("启用meme")
    async def remove_supervisor(
        self, event: AstrMessageEvent, meme_name: str | None = None
    ):
        """启用meme"""
        if not meme_name:
            yield event.plain_result("未指定要禁用的meme")
            return
        if not self.manager.is_meme_keyword(meme_name):
            yield event.plain_result(f"meme: {meme_name} 不存在")
            return
        if meme_name not in self.conf["memes_disabled_list"]:
            yield event.plain_result(f"meme: {meme_name} 未被禁用")
            return
        self.conf["memes_disabled_list"].remove(meme_name)
        self.conf.save_config()
        yield event.plain_result(f"已禁用meme: {meme_name}")

    @filter.command("meme黑名单")
    async def list_supervisors(self, event: AstrMessageEvent):
        """查看禁用的meme"""
        yield event.plain_result(f"当前禁用的meme: {self.conf['memes_disabled_list']}")

    @filter.event_message_type(EventMessageType.ALL, priority=10)
    async def meme_handle(self, event: AstrMessageEvent):
        """
        处理 meme 生成的主流程
        """
        message_str = (event.message_str or "").strip()
        if not message_str:
            return
 
        extra_prefix = str(self.conf.get("extra_prefix") or "").strip()
        if self.conf["need_prefix"] and not event.is_at_or_wake_command:
            if not ((extra_prefix and message_str.startswith(extra_prefix)) or message_str.startswith("#")):
                return
 
        param = message_str
        is_triggered = not self.conf["need_prefix"]
        triggered_by_prefix = False
        triggered_by_wake = False
 
        prefixes: list[str] = []
        if extra_prefix:
            prefixes.append(extra_prefix)
        if "#" not in prefixes:
            prefixes.append("#")
 
        for p in prefixes:
            if p and message_str.startswith(p):
                param = message_str[len(p) :].strip()
                is_triggered = True
                triggered_by_prefix = True
                break
 
        if event.is_at_or_wake_command:
            is_triggered = True
            triggered_by_wake = True
 
        if not is_triggered or not param:
            return
 
        if triggered_by_prefix:
            try:
                setattr(event, "is_command", True)
                setattr(event, "meme_triggered", True)
            except Exception:
                pass
 
        triggered_by_no_prefix = (
            not self.conf["need_prefix"]
            and not triggered_by_prefix
            and not triggered_by_wake
        )
        if triggered_by_no_prefix and not self.conf["fuzzy_match"]:
            parts = param.split()
            first = parts[0] if parts else ""
            if not first or first not in getattr(self.manager, "meme_keyword_set", set()):
                return
 
        keyword = self.manager.match_meme_keyword(
            text=param, fuzzy_match=self.conf["fuzzy_match"]
        )
        if not keyword or keyword in self.conf["memes_disabled_list"]:
            return

        # 合成表情
        try:
            image = await asyncio.wait_for(
                self.manager.generate_meme(event, keyword),
                timeout=self.conf["meme_timeout"],
            )
        except asyncio.TimeoutError:
            logger.warning(f"meme生成超时: {keyword}")
            yield event.plain_result("meme生成超时")
            return
        except Exception as e:
            logger.error(f"meme生成异常: {e}")
            return

        if image and self.conf["is_compress_image"]:
            try:
                image = compress_image(image) or image
            except Exception:
                pass

        if image and is_effectively_black_image(image):
            logger.warning(f"Rejected effectively black meme output: {keyword}")
            return

        if image:
            image_path = save_image_temp(image, "meme")
            yield event.chain_result([Comp.Image.fromFileSystem(image_path)])  # type: ignore
 
    async def terminate(self):
        """插件终止时清理调度器"""
        await self.collector.close()
