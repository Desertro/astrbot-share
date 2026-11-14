from __future__ import annotations
import asyncio
import base64
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any
import astrbot.core.message.components as Comp
from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.tool import FunctionTool, ToolSet
from .anima_prompt import ANIMA_PROMPT_SCHEMA, ANIMA_TOOL_DESCRIPTION, validate_direct_prompt
from .providers import MemeProvider, ShoubanhuaProvider
from .registry import CapabilityRegistry, CapabilityRequest
_NATIVE_IMAGE_HANDLERS = {'on_bnn', 'on_bna', 'on_gpt', 'on_resolution_bnn', 'on_nano', 'on_nano_without_prefix'}
_EXPLICIT_RESOLUTION_RE = re.compile('(?i)(?<![a-z0-9])(?:1k|2k|4k)(?![a-z0-9])|高清|高分辨率|(?<![a-z0-9])\\d{2,5}x\\d{2,5}(?![a-z0-9])')
_INTEROP_DIRECTORY = Path('/run/WSL')

class AnimaClient:
    """Minimal WSL-to-Windows ComfyUI bridge used directly by Alice."""

    def __init__(self, agent: 'CapabilityAgentPlugin'):
        self.agent = agent

    def _c(self, key: str, default: Any) -> Any:
        return self.agent._c(key, default)

    async def _windows_interop_env(self, executable: str) -> dict[str, str] | None:
        if not _INTEROP_DIRECTORY.is_dir():
            return None
        sockets = []
        for socket in _INTEROP_DIRECTORY.glob('*_interop'):
            try:
                sockets.append((socket.stat().st_mtime, socket))
            except OSError:
                continue
        candidates = [_INTEROP_DIRECTORY / '1_interop']
        candidates.extend((socket for _, socket in sorted(sockets, reverse=True)))
        inherited = os.environ.get('WSL_INTEROP')
        if inherited:
            candidates.append(Path(inherited))
        for socket in list(dict.fromkeys(candidates))[:4]:
            if not socket.is_socket():
                continue
            env = {**os.environ, 'WSL_INTEROP': str(socket)}
            probe = await asyncio.create_subprocess_exec(executable, '-c', "print('ANIMA_INTEROP_READY')", env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, _ = await asyncio.wait_for(probe.communicate(), timeout=3)
            except TimeoutError:
                if probe.returncode is None:
                    probe.kill()
                    await probe.wait()
                continue
            except BaseException:
                if probe.returncode is None:
                    probe.kill()
                    await probe.wait()
                raise
            if probe.returncode == 0 and out.strip() == b'ANIMA_INTEROP_READY':
                logger.debug('[anima] Windows interop ready via %s', socket)
                return env
        raise RuntimeError('Windows interop is unavailable; no ComfyUI job was submitted.')

    async def worker(self, payload: dict) -> dict:
        bridge = Path(__file__).with_name('anima_bridge.py')
        worker_env = None
        if sys.platform == 'win32':
            executable, script = (sys.executable, str(bridge))
        else:
            executable = str(self._c('anima_windows_python', ''))
            if not executable or not Path(executable).is_file():
                raise RuntimeError('Anima Windows Python is not configured or available.')
            converter = await asyncio.create_subprocess_exec('wslpath', '-w', str(bridge), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await converter.communicate()
            if converter.returncode:
                raise RuntimeError('Cannot resolve the Windows bridge path.')
            script = out.decode().strip()
            worker_env = await self._windows_interop_env(executable)
        payload = {**payload, 'base_url': self._c('anima_comfy_url', __import__('share_runtime').resolve('{{AB_CAPABILITY_AGENT_ANIMA_COMFY_URL}}')), 'timeout': int(self._c('anima_timeout_sec', 1800))}
        process = await asyncio.create_subprocess_exec(executable, script, env=worker_env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(json.dumps(payload, ensure_ascii=True).encode()), timeout=payload['timeout'] + 15)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode or not out:
            raise RuntimeError('Windows ComfyUI worker failed: ' + err.decode(errors='replace')[-300:])
        result = json.loads(out.decode('utf-8-sig'))
        if not result.get('ok'):
            raise RuntimeError(str(result.get('error', 'Unknown ComfyUI error')))
        return result

    async def generate(self, request) -> str:
        event = request.event
        if not self._c('anima_enabled', False):
            return '本地 Anima 生图尚未启用。'
        if event.get_extra('_anima_attempted', False):
            return '本轮已处理过 Anima 请求，不会重复提交。'
        request_text = str(request.args.get('prompt', '') or '').strip()
        if not request_text:
            return 'Anima 需要绘图要求，例如：画一张爱丽丝在窗边看书的 CG。'
        if re.search('改图|图生图|参考(?:这张|引用|当前).*图|使用(?:当前|引用).*图|image.to.image|edit.*image', request_text, re.I):
            return '当前 Anima 工作流支持文生图，不能读取参考图进行改图；请明确选择支持图生图的引擎。'
        max_chars = int(self._c('anima_max_request_chars', 0) or 0)
        if max_chars > 0 and len(request_text) > max_chars:
            return f'Anima 绘图要求请控制在 {max_chars} 字符以内。'
        resolution = str(request.args.get('resolution', 'default') or 'default').lower()
        width = int(self._c('anima_width', 1216))
        height = int(self._c('anima_height', 832))
        custom = re.fullmatch('(\\d{2,5})x(\\d{2,5})', resolution)
        if custom:
            width, height = (int(custom.group(1)), int(custom.group(2)))
        elif resolution == '1k':
            width, height = (1216, 832)
        elif resolution == '2k':
            width, height = (1536, 1024)
        elif resolution == '4k':
            width, height = (3840, 2560)
        if not (64 <= width <= 8192 and 64 <= height <= 8192):
            return 'Anima 画幅配置需在 64-8192 像素之间。'
        image_prompt = request.args.get('image_prompt')
        if not isinstance(image_prompt, dict):
            return '请由爱丽丝直接调用 anima_generate，填写最终 prompt。'
        direct_text = image_prompt.get('prompt')
        if not isinstance(direct_text, str) or not direct_text.strip():
            return '请由爱丽丝直接调用 anima_generate，填写最终 prompt。'
        sensitivity = str(image_prompt.get('sensitivity', 'safe') or 'safe')
        event.set_extra('_anima_attempted', True)
        started = time.monotonic()
        try:
            final_prompt = validate_direct_prompt(direct_text, request_text, False, sensitivity)
            await event.send(event.plain_result('爱丽丝正在用本地 Anima 画图，请稍等。'))
            result = await self.worker({'action': 'generate', 'workflow': self._c('anima_workflow', ''), 'mode': 'direct', 'prompt': final_prompt, 'style': '', 'characters': '', 'scene': final_prompt, 'width': width, 'height': height, 'max_queue': int(self._c('anima_max_queue', 0) or 0)})
            raw = base64.b64decode(result['image'], validate=True)
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('生成服务没有返回有效 PNG。')
            elapsed = time.monotonic() - started
            await event.send(event.chain_result([Comp.Image.fromBytes(raw), Comp.Plain(f'Anima · {width}×{height} · {elapsed:.1f}s')]))
            return 'Anima 图片已生成并发送。'
        except Exception as error:
            logger.warning('[anima] Generation failed: %s: %s', type(error).__name__, str(error)[:500])
            if isinstance(error, (ValueError, RuntimeError)):
                return 'Anima 未完成：' + str(error)[:250]
            return 'Anima 未完成：' + type(error).__name__ + '，请检查模型或本地服务。'
PLUGIN_AGENT_DESCRIPTION = '插件执行代理，负责 Aris 已安装的 meme/表情包与手办化生图插件。用户要求搜索或制作 meme、梗图、表情包，或进行手办化、AI 生图、图生图、改图、图片风格生成时，必须调用。用户询问这些插件支持哪些预设、指令、用法、状态、额度或模型时，也必须调用；不要凭记忆回答插件能力。task 要写完整且自包含；如果用户说“这张图/刚才那张”，写成“使用当前消息图片”“使用引用消息图片”或“使用本会话最近图片”，不要填写图片 URL/base64。本地 Anima/ComfyUI 文生图和普通 CG 必须由当前对话直接调用 anima_generate，最终 positive prompt 由你一次写完并原样提交；不要调用本代理进行规划。该代理会直接执行插件并可能直接发送图片。普通聊天、纯知识问答、代码问题不要调用。'
MANAGER_SYSTEM_PROMPT = '你是Aris的插件执行代理，不负责闲聊，只负责把用户任务变成白名单插件的具体调用。\n\n必须遵守：\n1. 先调用 capability_catalog 查看可用 capability、action 和 arguments_schema。domain 可填 auto、meme、image。\n2. 只能调用 catalog 返回过的 capability 和 action，不要猜测或编造。\n3. 参数不齐时先说明缺少什么；不要用随机内容补齐。\n4. 图片由执行器从当前消息、引用消息或本会话图片缓存直接读取。不要索取图片 URL，不要把 URL、base64 或历史图塞进参数。\n5. 生成类任务确认参数后调用生成动作；成功后不要重复生成或重复发送。未明确指定生图引擎时按 default_image_engine 选择；Anima/ComfyUI/本地CG 应由外层爱丽丝直接调用 anima_generate，不在本代理中规划或调用 image.generate。参考图编辑使用 gpt，或用户明确指定的 bnn。不得把参考图编辑降为 Anima 文生图。\n6. 除非用户明确要求 1K/2K/4K/高清，否则 resolution 必须使用 default，不要自行提升分辨率。\n7. 工具可能已经把图片直接发到聊天里，不要重复发送。\n8. 你看不到图片内容，不要描述生成结果长什么样，也不要声称检查过图片。\n9. 最终只回一句简短中文结果，例如“已生成并发送”“没找到这个 meme”“缺少关键词”。\n10. 不执行 catalog 之外的文件、shell、账号或管理操作。\n\n常见流程：\n- “做个某某梗图/表情包” -> catalog(domain=meme) -> 关键词不确定时 search -> generate。\n- “把这张图做成某某风格/bnn/gpt” -> catalog(domain=image) -> image.generate，prompt 写用户的风格要求。\n- “有哪些生图预设” -> image.presets。\n'

@register('astrbot_plugin_capability_agent', 'dsh', 'Aris的插件执行代理：本地Anima、memelite与手办化生图', '0.2.0')
class CapabilityAgentPlugin(Star):

    def __init__(self, context: Context, config: dict | None=None):
        super().__init__(context)
        self.conf = config or {}
        self.registry = CapabilityRegistry()
        self.anima = AnimaClient(self)
        for provider_cls in (MemeProvider, ShoubanhuaProvider):
            self.registry.register_provider(provider_cls(self))
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        if bool(self.conf.get('enabled', True)):
            self.context.add_llm_tools(FunctionTool(name='plugin_agent', description=PLUGIN_AGENT_DESCRIPTION, parameters={'type': 'object', 'properties': {'task': {'type': 'string', 'description': '完整、自包含的插件任务。保留“当前图片/引用图片”语义，但不要放图片 URL。'}, 'domain': {'type': 'string', 'enum': ['auto', 'meme', 'image'], 'default': 'auto', 'description': '可选的领域提示：表情包用 meme，AI 生图/手办化用 image。'}}, 'required': ['task'], 'additionalProperties': False}, handler=type(self)._handle_plugin_agent), FunctionTool(name='anima_generate', description=ANIMA_TOOL_DESCRIPTION, parameters={**ANIMA_PROMPT_SCHEMA, 'properties': {**ANIMA_PROMPT_SCHEMA['properties'], 'resolution': {'type': 'string', 'pattern': '^(default|none|1k|2k|4k|\\d{2,5}x\\d{2,5})$', 'default': 'default', 'description': 'default/1k/2k/4k，或宽x高，如 3840x2560'}}}, handler=type(self)._handle_anima_generate))
        self.manager_tools = ToolSet([FunctionTool(name='capability_catalog', description='查询当前允许的插件能力、动作和参数定义。第一次处理任务前必须先调用。query 可写用户需求关键词，domain 可填 auto/meme/image。', parameters={'type': 'object', 'properties': {'query': {'type': 'string', 'default': ''}, 'domain': {'type': 'string', 'enum': ['auto', 'meme', 'image'], 'default': 'auto'}}, 'additionalProperties': False}, handler=self._handle_catalog), FunctionTool(name='capability_call', description='执行 capability_catalog 返回的白名单动作。arguments 必须是对应 action 的 arguments_schema。', parameters={'type': 'object', 'properties': {'capability': {'type': 'string'}, 'action': {'type': 'string'}, 'arguments': {'type': 'object', 'description': '按 catalog 的 arguments_schema 填写。', 'additionalProperties': True}}, 'required': ['capability', 'action'], 'additionalProperties': False}, handler=self._handle_capability_call)])

    def _c(self, key: str, default: Any) -> Any:
        value = self.conf.get(key, default)
        return default if value is None else value

    def _admin_uids(self) -> set[str]:
        raw = self._c('admin_uids', [])
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(',') if part.strip()]
        return {str(item).strip() for item in raw}

    def _enabled_capabilities(self) -> set[str]:
        raw = self._c('enabled_capabilities', ['meme', 'image'])
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(',') if part.strip()]
        return {str(item).strip().lower() for item in raw}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return str(event.get_sender_id() or '') in self._admin_uids()

    def _group_gate(self, event: AstrMessageEvent) -> str | None:
        group_id = str(event.get_group_id() or '')
        if not group_id:
            if not bool(self._c('allow_private', True)):
                return '插件代理未对私聊启用。'
            return None
        enabled_groups = self._c('enabled_groups', [])
        if isinstance(enabled_groups, str):
            enabled_groups = [part.strip() for part in enabled_groups.split(',') if part.strip()]
        groups = {str(item).strip() for item in enabled_groups}
        if groups and group_id not in groups:
            return '插件代理未对本群启用。'
        return None

    @staticmethod
    def _has_image(event: AstrMessageEvent) -> bool:
        return any((isinstance(part, Comp.Image) for part in event.get_messages()))

    @staticmethod
    def _has_quoted_image(event: AstrMessageEvent) -> bool:
        for part in event.get_messages():
            if isinstance(part, Comp.Reply):
                if any((isinstance(item, Comp.Image) for item in list(part.chain or []))):
                    return True
        return False

    @staticmethod
    def _explicit_resolution_requested(event: AstrMessageEvent) -> bool:
        """Whether the current user text explicitly asks for a resolution."""
        try:
            text = str(getattr(event, 'message_str', '') or '')
        except Exception:
            return False
        return bool(_EXPLICIT_RESOLUTION_RE.search(text))

    @staticmethod
    def _native_image_handled_event(event: AstrMessageEvent) -> bool:
        """Whether a native bnn/bna/gpt handler already owns this turn."""
        try:
            handlers = event.get_extra('activated_handlers', []) or []
        except Exception:
            return False
        for handler in handlers:
            module_path = str(getattr(handler, 'handler_module_path', '') or '').lower()
            handler_name = str(getattr(handler, 'handler_name', '') or '')
            if 'astrbot_plugin_shoubanhua' in module_path and handler_name in _NATIVE_IMAGE_HANDLERS:
                return True
        return False

    async def _provider_id(self, event: AstrMessageEvent) -> str:
        configured = str(self._c('provider_id', '') or '').strip()
        if configured:
            return configured
        return await self.context.get_current_chat_provider_id(event.unified_msg_origin)

    async def _handle_plugin_agent(self, event: AstrMessageEvent, task: str, domain: str='auto') -> str:
        task_text = str(task or '').strip()
        if not task_text:
            return '插件代理缺少 task。'
        denied = self._group_gate(event)
        if denied:
            return denied
        max_chars = int(self._c('max_task_chars', 2000) or 2000)
        task_text = task_text[:max_chars]
        domain = str(domain or 'auto').strip().lower()
        if domain not in {'auto', 'meme', 'image'}:
            domain = 'auto'
        local_image_task = not re.search('\\b(?:gpt|bnn|bna)\\b|预设|状态|额度|模型|用法|帮助|支持|改图|图生图|参考', task_text, re.I) and bool(re.search('anima|comfyui|本地.*(?:生图|画图|绘图|CG)', task_text, re.I) or (domain == 'image' and str(self._c('default_image_engine', 'gpt')).lower() in {'anima', 'comfyui'}))
        if local_image_task:
            return '本地生图请直接调用 anima_generate，由你填写最终人物与场景提示词，无需插件代理规划。'
        context_bits = [f'domain_hint={domain}', 'current_message_has_image=' + ('yes' if self._has_image(event) else 'no'), 'quoted_message_has_image=' + ('yes' if self._has_quoted_image(event) else 'no'), f'default_image_engine={self._c('default_image_engine', 'gpt')}']
        prompt = f'用户任务：\n{task_text}\n\n执行上下文（仅用于判断图片引用，不是图片内容）：\n' + '\n'.join(context_bits)
        logger.info('[capability_agent] task domain=%s group=%s sender=%s chars=%s', domain, event.get_group_id() or 'private', event.get_sender_id(), len(task_text))
        lock_key = event.unified_msg_origin
        try:
            async with self._locks[lock_key]:
                response = await self.context.tool_loop_agent(event=event, chat_provider_id=await self._provider_id(event), prompt=prompt, tools=self.manager_tools, system_prompt=MANAGER_SYSTEM_PROMPT, max_steps=int(self._c('max_steps', 6) or 6), tool_call_timeout=int(self._c('inner_tool_timeout_sec', 420) or 420))
            text = str(getattr(response, 'completion_text', '') or '').strip()
        except Exception as exc:
            logger.exception('[capability_agent] manager run failed')
            return f'插件代理执行失败：{type(exc).__name__}: {exc}'
        limit = int(self._c('answer_max_chars', 600) or 600)
        return (text or '插件代理已完成，但没有返回文字结果。')[:limit]

    async def _handle_anima_generate(self, event: AstrMessageEvent, prompt: str='', characters: list[dict[str, Any]] | None=None, scene_and_action_prompt: str='', sensitivity: str='safe', resolution: str='default') -> str:
        """Submit the conversation model's final prompt to ComfyUI unchanged.

        Args:
            event: Real originating chat event.
            prompt: Complete final positive prompt produced by the conversation model.
            characters: Deprecated compatibility parameter; ignored in direct mode.
            scene_and_action_prompt: Legacy fallback used when prompt is empty.
            sensitivity: Legacy content label; unused since content validation is removed.
            resolution: Requested output size, subject to the ordinary size guard.

        Returns:
            Actual generation or validation result.
        """
        final_prompt = str(prompt or scene_and_action_prompt or '').strip()
        return await self._handle_capability_call(event, 'image', 'generate', {'engine': 'anima', 'prompt': str(event.message_str or '').strip() or final_prompt, 'image_prompt': {'prompt': final_prompt, 'sensitivity': sensitivity}, 'resolution': resolution})

    @filter.command('anima')
    async def on_anima(self, event: AstrMessageEvent):
        """Generate a local CG through the same gated capability execution path.

        Args:
            event: Real originating command event.
        """
        event.stop_event()
        prompt = re.sub('^#?anima\\s*', '', event.message_str or '', flags=re.I).strip()
        result = await self._handle_anima_generate(event, prompt=prompt)
        if result != 'Anima 图片已生成并发送。':
            yield event.plain_result(result)

    @filter.command('anima状态')
    async def on_anima_status(self, event: AstrMessageEvent):
        """Inspect the Windows ComfyUI endpoint without generating an image.

        Args:
            event: Real originating command event.
        """
        event.stop_event()
        denied = self._group_gate(event)
        if denied:
            yield event.plain_result(denied)
            return
        try:
            result = await self.anima.worker({'action': 'status'})
            text = f'Anima / ComfyUI {result['version']}，运行 {result['running']}，排队 {result['pending']}。'
        except Exception as error:
            text = 'Anima 服务不可用：' + str(error)[:180]
        yield event.plain_result(text)

    async def _handle_catalog(self, event: AstrMessageEvent, query: str='', domain: str='auto') -> str:
        enabled = self._enabled_capabilities()
        catalog = self.registry.catalog(query=query, domain=domain, enabled=enabled)
        return json.dumps(catalog, ensure_ascii=False, indent=2)

    async def _handle_capability_call(self, event: AstrMessageEvent, capability: str, action: str, arguments: dict[str, Any] | str | None=None) -> str:
        denied = self._group_gate(event)
        if denied:
            return denied
        capability = str(capability or '').strip().lower()
        action = str(action or '').strip().lower()
        if capability not in self._enabled_capabilities():
            return f'能力未启用：{capability}'
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return 'arguments 不是合法 JSON。'
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return 'arguments 必须是对象。'
        if capability == 'image' and action == 'generate' and self._native_image_handled_event(event):
            logger.info('[capability_agent] duplicate image generation blocked: native shoubanhua handler already owns this turn')
            return '该请求已由手办化原生指令执行，本轮不会重复生成图片。'
        if capability == 'image' and action == 'generate':
            engine = str(arguments.get('engine', '') or '').strip().lower()
            if engine in {'', 'auto', 'default'}:
                arguments['engine'] = str(self._c('default_image_engine', 'gpt'))
            requested_resolution = str(arguments.get('resolution', 'default') or 'default').lower()
            if requested_resolution not in {'', 'default', 'none'} and (not self._explicit_resolution_requested(event)):
                logger.info('[capability_agent] resolution downgraded to default: user did not explicitly request %s', requested_resolution)
                arguments['resolution'] = 'default'
        logger.info('[capability_agent] call capability=%s action=%s group=%s sender=%s', capability, action, event.get_group_id() or 'private', event.get_sender_id())
        spec = self.registry.get(capability, action)
        if not spec:
            return f'未知能力或动作：{capability}.{action}。请先调用 capability_catalog 查看白名单。'
        try:
            import jsonschema
            jsonschema.validate(arguments, spec.parameters)
        except Exception as exc:
            return f'arguments 不符合 {capability}.{action} 的参数定义：{exc}'
        request = CapabilityRequest(event=event, args=arguments, is_admin=self._is_admin(event), agent=self)
        try:
            return await self.registry.execute(capability, action, request)
        except Exception as exc:
            logger.exception('[capability_agent] action failed capability=%s action=%s', capability, action)
            return f'插件动作失败：{capability}.{action} ({type(exc).__name__})'

    async def send_image_bytes(self, event: AstrMessageEvent, image: bytes) -> None:
        await event.send(event.chain_result([Comp.Image.fromBytes(image)]))

    @filter.command('插件代理状态', prefix_optional=True)
    async def capability_agent_status(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result('权限不足：插件代理状态仅限管理员。')
            return
        enabled = self._enabled_capabilities()
        lines = ['插件能力代理状态：已加载', f'启用能力：{', '.join(sorted(enabled)) or '无'}', f'Provider：{self._c('provider_id', '') or '当前聊天模型'}']
        for item in self.registry.catalog(enabled=enabled):
            lines.append(f'- {item['capability']}：' + '、'.join((action['action'] for action in item['actions'])))
        yield event.plain_result('\n'.join(lines))
