from __future__ import annotations

import importlib
from typing import Any

from astrbot.core.message.components import Image

from ..anima_prompt import ANIMA_PROMPT_SCHEMA
from ..registry import CapabilityRequest, CapabilitySpec
from .base import BaseCapabilityProvider

FREE_ENGINES = {"bnn", "bna", "gpt"}


class ShoubanhuaProvider(BaseCapabilityProvider):
    name = "image"
    description = (
        "AI 生图：anima/ComfyUI 本地文生图、gpt、bnn Gemini 系列、bna 纯文生图，"
        "以及插件内置的预设风格。会自动读取当前消息、历史图片或引用消息中的参考图。"
    )
    plugin_names = ("astrbot_plugin_shoubanhua", "shoubanhua")

    def build(self) -> CapabilitySpec:
        return CapabilitySpec(
            name=self.name,
            description=self.description,
            actions={
                "presets": self.action(
                    "presets",
                    "列出手办化插件可用的引擎和预设风格关键词。生成前可先查看。",
                    None,
                    self._presets,
                ),
                "status": self.action(
                    "status",
                    "查看当前手办化后端、默认模型和预设，不返回任何 API Key。",
                    None,
                    self._status,
                ),
                "help": self.action(
                    "help",
                    "查看手办化插件的完整帮助和指令说明。",
                    {
                        "type": "object",
                        "properties": {
                            "command_help": {
                                "type": "boolean",
                                "default": False,
                                "description": "true 查看管理指令帮助，false 查看普通帮助。",
                            }
                        },
                        "additionalProperties": False,
                    },
                    self._help,
                ),
                "generate": self.action(
                    "generate",
                    "执行 AI 生图。默认引擎由主机配置指定；anima/comfyui 是本地 Anima 文生图，gpt/bnn 支持参考图编辑，bna 纯文生图。",
                    {
                        "type": "object",
                        "properties": {
                            "engine": {
                                "type": "string",
                                "description": "anima/comfyui=本地文生图；gpt/bnn=支持参考图编辑；bna=纯文生图，也可填 presets 返回的预设。",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "完整生成要求。bnn/bna/gpt 必填；预设风格可留空。",
                            },
                            "image_prompt": ANIMA_PROMPT_SCHEMA,
                            "resolution": {
                                "type": "string",
                                "enum": ["default", "1k", "2k", "4k"],
                                "default": "default",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                    self._generate,
                ),
            },
        )

    def _prompts_module(self, plugin: Any) -> Any | None:
        try:
            package = type(plugin).__module__.rsplit(".", 1)[0]
            return importlib.import_module(f"{package}.prompts")
        except Exception:
            return None

    async def _presets(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        if not plugin:
            return self.missing_plugin()
        prompts = self._prompts_module(plugin)
        prompt_map = getattr(prompts, "prompt_map", None) if prompts else None
        if not isinstance(prompt_map, dict):
            prompt_map = {}
        lines = [
            "可用生图引擎/预设：",
            "anima / comfyui｜本地 Anima 文生图；提示词受 mem_chat NSFW 会话白名单约束",
            "bnn｜Gemini 系列，自动文生图/图生图",
            "bna｜纯文生图",
            "gpt｜GPT-Image 2.5，自动文生图/图生图",
        ]
        for key, value in prompt_map.items():
            if key in FREE_ENGINES:
                continue
            desc = ""
            if isinstance(value, dict):
                desc = str(value.get("description", "") or "").strip()
            lines.append(f"{key}｜{desc or '预设风格'}")
        return "\n".join(lines)

    async def _status(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        if not plugin:
            return self.missing_plugin()
        if bool(getattr(plugin, "conf", {}).get("use_local_zimage", False)):
            return "当前手办化后端：本地 Z-Image"
        try:
            backends = plugin._build_remote_backends()
        except Exception as exc:
            return f"读取手办化后端失败：{type(exc).__name__}: {exc}"
        if not backends:
            return "当前没有配置云端手办化后端。"
        lines = ["当前手办化后端："]
        for backend in backends:
            lines.append(
                f"- {backend.get('name', '未命名')}｜格式 {backend.get('api_format', '')}"
                f"｜bnn 模型 {backend.get('model_name', '')}"
                f"｜gpt 模型 {' -> '.join(backend.get('gpt_model_names') or [backend.get('gpt_model_name', '')])}"
            )
        return "\n".join(lines)

    async def _help(self, request: CapabilityRequest) -> str:
        plugin = self.require_plugin()
        if not plugin:
            return self.missing_plugin()
        prompts = self._prompts_module(plugin)
        command_help = bool(request.args.get("command_help", False))
        if prompts:
            getter = getattr(
                prompts,
                "get_command_help" if command_help else "get_help_text",
                None,
            )
            if callable(getter):
                return str(getter())
        return "手办化帮助不可用。"

    async def _generate(self, request: CapabilityRequest) -> str:
        engine = str(request.args.get("engine", "") or "").strip().lower()
        if engine in {"anima", "comfyui"}:
            return await self.agent.anima.generate(request)
        plugin = self.require_plugin()
        if not plugin:
            return self.missing_plugin()
        engine = str(request.args.get("engine", "") or "").strip().lower()
        if engine in {"", "auto", "default"}:
            engine = "gpt"
        prompt = str(request.args.get("prompt", "") or "").strip()
        resolution_raw = str(
            request.args.get("resolution", "default") or "default"
        ).lower()
        resolution = (
            None if resolution_raw in {"", "default", "none"} else resolution_raw
        )

        prompts = self._prompts_module(plugin)
        prompt_map = getattr(prompts, "prompt_map", None) if prompts else None
        if not isinstance(prompt_map, dict):
            prompt_map = {}
        if engine not in FREE_ENGINES and engine not in prompt_map:
            return f"未知生图引擎/预设：{engine}。请先调用 image.presets。"
        if engine in FREE_ENGINES and not prompt:
            return f"{engine} 需要 prompt。"
        if resolution not in {None, "1k", "2k", "4k"}:
            return f"不支持的分辨率：{resolution_raw}"

        sent_image = False
        final_chain = None
        try:
            async for result in plugin._process_figurine_request(
                request.event,
                engine,
                prompt,
                resolution=resolution,
            ):
                if getattr(result, "chain", None):
                    final_chain = list(result.chain)
                if self._result_has_image(result):
                    sent_image = True
                await request.event.send(result)
        except Exception as exc:
            return f"手办化生成失败：{type(exc).__name__}: {exc}"

        await self._record_activity(request.event, engine, sent_image, final_chain)
        if sent_image:
            return f"已生成并发送图片：{engine}"
        return f"已执行 {engine}，但没有生成图片；请检查上方错误提示。"

    async def _record_activity(
        self,
        event: Any,
        engine: str,
        sent_image: bool,
        chain: list[Any] | None,
    ) -> None:
        mem_chat = self.resolve_plugin_by_names(("mem_chat", "astrbot_plugin_mem_chat"))
        recorder = getattr(mem_chat, "record_plugin_activity", None)
        activity = event.get_extra("_mem_chat_shoubanhua_activity", {}) or {}
        if not callable(recorder) or not isinstance(activity, dict):
            return
        try:
            await recorder(
                event,
                plugin="shoubanhua",
                status="sent" if sent_image else "failed",
                summary=(
                    f"{engine} 图片生成成功。"
                    if sent_image
                    else f"{engine} 图片生成失败。"
                ),
                chain=chain,
                reference_images=activity.get("reference_images"),
            )
            event.set_extra("_mem_chat_shoubanhua_activity", None)
        except Exception:
            return

    @staticmethod
    def _result_has_image(result: Any) -> bool:
        chain = getattr(result, "chain", None) or []
        return any(isinstance(part, Image) for part in chain)
