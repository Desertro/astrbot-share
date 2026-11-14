from __future__ import annotations

import asyncio
import importlib
from typing import Any

import astrbot.core.message.components as Comp

from ..registry import CapabilityRequest, CapabilitySpec
from .base import BaseCapabilityProvider


class MemeProvider(BaseCapabilityProvider):
    name = "meme"
    description = (
        "memelite 表情包能力：搜索、查看详情、生成 meme、查看指令和黑名单管理。"
    )
    plugin_names = ("astrbot_plugin_memelite", "memelite")

    def build(self) -> CapabilitySpec:
        return CapabilitySpec(
            name=self.name,
            description=self.description,
            actions={
                "search": self.action(
                    "search",
                    "按关键词、别名或标签搜索可用的 meme。返回文字列表，不生成图片。",
                    {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "搜索词；留空表示列出前若干个 meme。",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "default": 20,
                            },
                        },
                        "additionalProperties": False,
                    },
                    self._search,
                ),
                "detail": self.action(
                    "detail",
                    "查看某个 meme 的关键词、图片数量、文字数量、参数和触发示例。必要时可发送预览图。",
                    {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "meme 的准确关键词或别名。",
                            },
                            "show_preview": {
                                "type": "boolean",
                                "default": False,
                                "description": "是否把预览图直接发到当前聊天。",
                            },
                        },
                        "required": ["keyword"],
                        "additionalProperties": False,
                    },
                    self._detail,
                ),
                "generate": self.action(
                    "generate",
                    "用 memelite 生成表情包。只能使用 search/detail 确认过的关键词；会读取当前消息或引用消息里的图片。",
                    {
                        "type": "object",
                        "properties": {
                            "keyword": {
                                "type": "string",
                                "description": "要生成的 meme 关键词。",
                            },
                            "parameters": {
                                "type": "string",
                                "description": "关键词后面的文字和选项，例如“你好”或“第一段|第二段 -n 3”。不要填写关键词本身。",
                            },
                        },
                        "required": ["keyword"],
                        "additionalProperties": False,
                    },
                    self._generate,
                ),
                "commands": self.action(
                    "commands",
                    "列出 memelite 的原始用户指令和生成用法。",
                    None,
                    self._commands,
                ),
                "blacklist": self.action(
                    "blacklist",
                    "查看当前被禁用的 meme 关键词。",
                    None,
                    self._blacklist,
                ),
                "disable": self.action(
                    "disable",
                    "禁用一个 meme。仅管理员可用。",
                    {
                        "type": "object",
                        "properties": {"keyword": {"type": "string"}},
                        "required": ["keyword"],
                        "additionalProperties": False,
                    },
                    self._disable,
                ),
                "enable": self.action(
                    "enable",
                    "重新启用一个 meme。仅管理员可用。",
                    {
                        "type": "object",
                        "properties": {"keyword": {"type": "string"}},
                        "required": ["keyword"],
                        "additionalProperties": False,
                    },
                    self._enable,
                ),
            },
        )

    def _manager(self) -> Any | None:
        plugin = self.require_plugin()
        if not plugin:
            return None
        return getattr(plugin, "manager", None)

    async def _search(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        manager = self._manager()
        if not plugin or not manager:
            return self.missing_plugin()
        query = str(request.args.get("query", "") or "").strip()
        try:
            limit = max(1, min(50, int(request.args.get("limit", 20) or 20)))
        except (TypeError, ValueError):
            limit = 20
        return str(manager.format_meme_search_result(query, limit))

    async def _detail(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        manager = self._manager()
        if not plugin or not manager:
            return self.missing_plugin()
        keyword = str(request.args.get("keyword", "") or "").strip()
        if not keyword:
            return "缺少 keyword。"
        info = manager.get_meme_info(keyword)
        if not info:
            return f"没有找到 meme：{keyword}"
        text, preview = info
        if bool(request.args.get("show_preview", False)) and isinstance(preview, bytes):
            await self.agent.send_image_bytes(request.event, preview)
        return str(text)

    async def _generate(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        manager = self._manager()
        if not plugin or not manager:
            return self.missing_plugin()

        keyword = str(request.args.get("keyword", "") or "").strip()
        parameters = str(request.args.get("parameters", "") or "").strip()
        if not keyword:
            return "缺少 keyword。"
        if not manager.find_meme(keyword):
            return f"没有找到 meme：{keyword}。请先调用 meme.search 确认关键词。"
        disabled = getattr(plugin, "conf", {}).get("memes_disabled_list", [])
        if keyword in disabled:
            return f"meme 已被禁用：{keyword}"

        message_obj = getattr(request.event, "message_obj", None)
        original_chain = getattr(message_obj, "message", None)
        carried = [
            segment
            for segment in request.event.get_messages()
            if not isinstance(segment, Comp.Plain)
        ]
        command_line = f"{keyword} {parameters}".strip()

        timeout_raw = getattr(plugin, "conf", {}).get("meme_timeout", 30)
        try:
            timeout = max(5.0, float(timeout_raw))
        except (TypeError, ValueError):
            timeout = 30.0

        try:
            if message_obj is not None:
                message_obj.message = [Comp.Plain(command_line), *carried]
            image = await asyncio.wait_for(
                manager.generate_meme(request.event, keyword),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return f"meme 生成超时：{keyword}"
        except Exception as exc:
            return f"meme 生成失败：{type(exc).__name__}: {exc}"
        finally:
            if message_obj is not None and original_chain is not None:
                message_obj.message = original_chain

        if not isinstance(image, bytes) or not image:
            return f"meme 没有生成图片：{keyword}"

        image = self._optimize_image(plugin, image)
        if not image:
            return f"meme 生成结果异常，已丢弃：{keyword}"

        await self.agent.send_image_bytes(request.event, image)
        return f"已生成并发送 meme：{keyword}"

    def _optimize_image(self, plugin: Any, image: bytes) -> bytes | None:
        try:
            package = type(plugin).__module__.rsplit(".", 1)[0]
            utils = importlib.import_module(f"{package}.utils")
            if bool(getattr(plugin, "conf", {}).get("is_compress_image", True)):
                image = utils.compress_image(image) or image
            if utils.is_effectively_black_image(image):
                return None
            return image
        except Exception:
            return image

    async def _commands(self, request: CapabilityRequest) -> str:
        manager = self._manager()
        if not manager:
            return self.missing_plugin()
        return str(manager.format_meme_commands())

    async def _blacklist(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        if not plugin:
            return self.missing_plugin()
        disabled = getattr(plugin, "conf", {}).get("memes_disabled_list", [])
        return f"当前禁用的 meme：{list(disabled)}"

    async def _disable(self, request: CapabilityRequest) -> str:
        if not request.is_admin:
            return "权限不足：禁用 meme 仅限管理员。"
        plugin = self.require_plugin()
        manager = self._manager()
        if not plugin or not manager:
            return self.missing_plugin()
        keyword = str(request.args.get("keyword", "") or "").strip()
        if not keyword:
            return "缺少 keyword。"
        if not manager.is_meme_keyword(keyword):
            return f"meme 不存在：{keyword}"
        disabled = getattr(plugin, "conf", {}).get("memes_disabled_list", [])
        if keyword in disabled:
            return f"meme 已经被禁用：{keyword}"
        disabled.append(keyword)
        plugin.conf["memes_disabled_list"] = disabled
        plugin.conf.save_config()
        return f"已禁用 meme：{keyword}"

    async def _enable(self, request: CapabilityRequest) -> str:
        if not request.is_admin:
            return "权限不足：启用 meme 仅限管理员。"
        plugin = self.require_plugin()
        manager = self._manager()
        if not plugin or not manager:
            return self.missing_plugin()
        keyword = str(request.args.get("keyword", "") or "").strip()
        if not keyword:
            return "缺少 keyword。"
        if not manager.is_meme_keyword(keyword):
            return f"meme 不存在：{keyword}"
        disabled = getattr(plugin, "conf", {}).get("memes_disabled_list", [])
        if keyword not in disabled:
            return f"meme 没有被禁用：{keyword}"
        disabled.remove(keyword)
        plugin.conf["memes_disabled_list"] = disabled
        plugin.conf.save_config()
        return f"已启用 meme：{keyword}"
