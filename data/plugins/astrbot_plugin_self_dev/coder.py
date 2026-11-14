"""A tiny function-calling loop for the coder agent.

Written against the OpenAI-compatible chat API instead of AstrBot's provider
stack so the coder keeps working when the bot's own provider is rate-limited
or out of quota. The loop is deliberately small: one assistant turn, run the
requested tools, feed the results back, repeat.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

Dispatch = Callable[[str, dict[str, Any]], Awaitable[str]]


def tool_spec(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


SPECS = [
    tool_spec(
        "sd_list",
        "列出工作区文件（相对路径）",
        {"path": {"type": "string", "description": "子目录，留空看根"}},
        [],
    ),
    tool_spec(
        "sd_read",
        "读取文件内容",
        {"path": {"type": "string", "description": "相对路径"}},
        ["path"],
    ),
    tool_spec(
        "sd_write",
        "覆盖写入文件",
        {
            "path": {"type": "string", "description": "相对路径"},
            "content": {"type": "string", "description": "完整文件内容"},
        },
        ["path", "content"],
    ),
    tool_spec("sd_check", "对工作区所有 py 文件做语法检查", {}, []),
    tool_spec(
        "sd_finish",
        "结束修改并给出总结",
        {"summary": {"type": "string", "description": "中文总结"}},
        ["summary"],
    ),
]


async def run_coder(
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_steps: int,
    system_prompt: str,
    user_prompt: str,
    dispatch: Dispatch,
    log,
) -> tuple[str, list[str]]:
    """Run the coder loop and return (summary, trace)."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=240, max_retries=1)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[str] = []
    summary = ""

    for step in range(max_steps):
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=SPECS,
            tool_choice="auto",
            temperature=temperature,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            summary = (msg.content or "").strip()
            break

        finished = False
        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = await dispatch(name, args)
            except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
                result = f"工具出错：{exc}"
            trace.append(f"step{step + 1} {name} -> {result[:100]}")
            log(f"[self_dev] {trace[-1]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result[:8000]})
            if name == "sd_finish":
                summary = str(args.get("summary", "")).strip()
                finished = True
        if finished:
            break

    return summary, trace
