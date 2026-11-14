# astrbot_plugin_capability_agent

Aris 主聊天使用 `anima_generate` 直接生成本地插画：人物与场景提示词由当前对话中的爱丽丝一次写完，直接提交 ComfyUI。`plugin_agent` 仍负责 meme、其他生图引擎和插件查询。

## 当前能力

### meme

- search: 搜索 meme
- detail: 查看参数和预览
- generate: 生成表情包
- commands: 原始指令说明
- blacklist/disable/enable: 黑名单管理

### image

- presets: 列出 bnn、bna、gpt 和预设风格
- status: 查看当前后端，不输出 Key
- help: 查看手办化帮助
- generate: 执行 bnn、bna、gpt 或预设生图

## 图片规则

子代理不会收到图片 URL 或 base64。它只看到 current_message_has_image 和 quoted_message_has_image，真正执行时由 memelite 或手办化插件从当前事件读取原图。这样既避免图片污染子代理上下文，也避免主模型把历史图误当成本次参考图。

## 后续扩展

1. 在 providers 目录新建一个 BaseCapabilityProvider 子类。
2. 在 build 中只暴露安全、语义明确的 action，不要直接把插件的全部原始命令批量暴露给模型。
3. 在 providers/__init__.py 和 main.py 的 provider 列表里注册。
4. 把 capability 名加入配置 enabled_capabilities。
5. 破坏性动作必须在 action 内检查 request.is_admin，并从 request.args 取结构化参数。

## Local Anima / ComfyUI

`image.generate` accepts `engine=anima` or `engine=comfyui` for text-to-image.
`default_image_engine` chooses the engine when none is specified. Reference-image
editing still needs an engine that supports image-to-image; Anima does not silently
ignore a requested reference image.

Commands use the deployment's `#` prefix:

- `#anima aris (blue archive), halo, school uniform, reading a book in a sunlit library`（原样提交提示词）
- `#anima状态`

Set `anima_enabled`, `anima_windows_python` (an executable WSL path),
`anima_comfy_url` (Windows loopback), and `anima_workflow` (a Windows-readable API
workflow). The bridge runs Windows Python through WSL interop and returns JSON over
stdio; it does not add a network listener or use the HTTP proxy for ComfyUI.
Before starting the worker, the backend probes Windows Python through a responsive
WSL interop socket, preferring the init socket over stale tmux environment values.
Only this harmless probe may retry; the generation worker is launched once to
avoid submitting duplicate jobs.
The workflow is an independent original-Anima copy under
`data/plugin_data/astrbot_plugin_capability_agent/anima/workflow.json`.
It must contain a positive `CLIPTextEncode` node with either
`CHATARCHIVE_SCENE` or `CHATARCHIVE_PROMPT`. Legacy templates may also contain
`CHATARCHIVE_STYLE` and `CHATARCHIVE_CHARACTERS`. The bridge randomizes the sampler
seed, sets batch size to one, and preserves the workflow's sampling settings.
Direct mode replaces the complete positive text with the supplied prompt, so
template style, character, and quality prefixes are intentionally not applied.
DeepSeek rewrite nodes are rejected. Prompt ownership stays with the current conversation model.

The current conversation model calls `anima_generate` with a final `prompt` and
`sensitivity`. The prompt is passed to ComfyUI unchanged; no manager, planner,
translator, rewriter, or reviewer LLM is invoked for this path. Legacy
`characters`/`scene_and_action_prompt` fields are still accepted for old callers,
but new calls should send only `prompt`. `#anima` submits manually written final
tags directly, without LLM processing.
Default size is 1216 x 832; explicit 2K uses 1536 x 1024. This integration does not
support 4K.

The prompt gate uses the live mem_chat `_is_adult_allowed(umo)` decision:
`scene_adult_mode`, `adult_group_whitelist`, and `adult_private_uids`. No additional NSFW whitelist is maintained. Missing mem_chat denies adult access.
The backend validates the supplied prompt, after
waiting for the GPU, and before delivery. Non-whitelisted requests remain
non-sexual. Blue Archive characters always remain non-sexual, regardless of the
session whitelist. Only clearly adult original fictional characters may use
non-graphic artistic nudity in an authorized session; graphic sexual content is
not supported.

One event can submit at most one Anima job. Output images are recorded through
mem_chat's existing plugin activity mechanism so later turns can refer to them.
Final prompts, whitelist decisions and job IDs are stored locally under
`data/plugin_data/astrbot_plugin_capability_agent/anima/jobs/` with mode 0600;
API keys are not included.

### Alice default visual prompt alignment (2026-11-13)

`ANIMA_TOOL_DESCRIPTION` in `anima_prompt.py` now supplies a tested default visual
baseline for Tendou Aris: the existing `aris (blue archive)` tag, very long navy
hair, blue eyes, an asymmetrical fringe and cyan square halo, plus her white/black
long coat, aqua necktie, black pleated skirt, ankle socks and white boots. It also
restores the early `blue archive style` / `very aesthetic` / `clean lineart` style
baseline. This is guidance for the current conversation model; direct mode still
submits its complete prompt verbatim, including manually supplied `#anima` tags.

Use the outfit baseline only if the request and established scene do not specify
a different outfit. Explicit outfit changes replace the default outfit; other
characters and multiple-character scenes keep their own identities and counts.
The default does not add a controller, weapon or unrelated scene details.

Validation uses local original portrait artwork and controlled nonsexual scene
comparisons on the existing Anima model and both current LoRAs. Full prompts,
seeds, output PNGs and comparison notes are archived in the Windows LocalImageAPI
`AlicePromptAlignment-20261113-163814` directory. The separate character corpus
bundle is still an offline source and is not dynamically loaded by this plugin.
