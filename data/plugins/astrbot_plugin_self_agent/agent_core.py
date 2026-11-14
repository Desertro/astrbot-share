"""A small read/run agent used by the self_agent plugin (phase-0 backend).

It is deliberately boring: a function-calling loop over four tools that can
look at and poke a whitelisted working directory. No writing, no installs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

Dispatch = Callable[[str, dict[str, Any]], Awaitable[str]]

# Commands that never get to run, no matter who asks.
BLOCKLIST = (
    "rm -rf /",
    "mkfs",
    "dd if=",
    "> /dev/",
    ":(){",
    "shutdown",
    "reboot",
    "pkill",
    "kill -9",
    "chmod 777 /",
    "passwd",
    "curl | sh",
    "wget | sh",
)

SYSTEM_PROMPT = """你是一个在别人服务器上干活的助手，只能通过工具观察和操作。

规则：
1. 先看再答。涉及系统状态的问题，必须真的跑命令，不要凭印象回答。
2. 命令要短、要只读：uptime、df -h、free -m、ls、cat、tail、grep、ps、systemctl status 都可以。
3. 不要执行安装、删除、重启、改配置这类会改变系统状态的操作；实在需要就说明原因并停下来。
4. 用群里聊天的口吻说话：短、口语，不要罗列你跑过哪些命令，也不要用工程报告腔
   （不要写"依据命令：""未做任何修改"这类话）。对方追问依据时再用一句话提一下。
5. 数字和文件名要具体，结论先行，中文，3 句话以内。
6. 完成后调用 finish。
"""

SPECS = [
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": "在受限目录执行一条只读 shell 命令",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string", "description": "命令，如 uptime"}},
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "读取文本文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "tail": {"type": "integer", "description": "只看最后 N 行"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list",
            "description": "列目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "给出最终回答",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "中文结论，200 字内"}},
                "required": ["answer"],
            },
        },
    },
]


async def _run_shell(cmd: str, cwd: Path, timeout: int = 30) -> str:
    low = cmd.lower()
    for bad in BLOCKLIST:
        if bad in low:
            return f"拒绝执行：命中黑名单片段 {bad!r}"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return f"启动失败：{exc}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"超时（>{timeout}s）：{cmd}"
    text = (out or b"").decode("utf-8", "replace").strip()
    return text[:4000] or "(无输出)"


async def judge_meme(
    *, base_url: str, api_key: str, model: str, text: str, log=None
) -> dict[str, str] | None:
    """Decide whether an exchange holds a meme worth remembering, and distil it.

    The bot curates its own knowledge base: ordinary chatter is dropped, while
    slang, running jokes, or an explained reference come back structured so they
    can be embedded later.

    Args:
        base_url: OpenAI-compatible endpoint.
        api_key: API key.
        model: Model name.
        text: The exchange to judge (user message plus the bot's answer).
        log: Optional logging callable.

    Returns:
        A dict with term/meaning/usage/example, or None when nothing is worth keeping.
    """
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=25, max_retries=0)
    system = (
        "你是一个梗知识库的守门人。看一段群聊，判断里面有没有值得长期记住的东西："
        "网络梗、黑话、群里固定说法、某个梗的出处或用法。"
        '有就输出 {"keep": true, "term": "梗的名字", "meaning": "意思", '
        '"usage": "什么场合用", "example": "例子或原话"}；'
        '普通闲聊、情绪、家常、纯提问就输出 {"keep": false}。只输出 JSON。'
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text[:2000]},
            ],
            temperature=0,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        msg = resp.choices[0].message
        raw = (msg.content or "").strip()
        if not raw:
            raw = str(msg.model_dump().get("reasoning_content") or "").strip()
        start = raw.find("{")
        data = json.loads(raw[start:]) if start >= 0 else {}
    except Exception as exc:  # noqa: BLE001 - curation must never break a reply
        if log:
            log(f"[self_agent] judge_meme error: {exc}")
        return None
    if not data.get("keep"):
        if log:
            log("[self_agent] judge_meme keep=False")
        return None
    item = {
        k: str(data.get(k, "") or "").strip()
        for k in ("term", "meaning", "usage", "example")
    }
    if not item["term"]:
        return None
    if log:
        log(f"[self_agent] judge_meme keep=True term={item['term']}")
    return item


async def classify_task(
    *, base_url: str, api_key: str, model: str, text: str,
    context: str = "", log=None
) -> bool:
    """Ask a cheap model whether this message is asking the bot to go do something.

    Args:
        base_url: OpenAI-compatible endpoint.
        api_key: API key.
        model: Model name.
        text: The message to judge.
        context: Optional quoted-message context for ambiguous short replies.

    Returns:
        True when the message is a work request, False for ordinary chat.
    """
    # Deterministic guard before the classifier.  A cheap model was treating
    # erotic role-play imperatives such as “去帮我舔舔” or “内射爱丽丝” as
    # work requests and handing them to the shell agent.  That agent then
    # replied in its own voice, which looked like a second personality.
    roleplay_or_flirt = re.compile(
        r"(涩涩|色色|色胚|调情|做爱|内射|射精|操你|操我|插死|插你|插我|插入|被插|顶死|顶昏|"
        r"胯下|舔|母狗|发骚|发情|抱抱|亲亲|摸摸|想看你|烧一点|"
        r"撸|🦌|小母狗|骚话|荤段子|擦边)"
    )
    if roleplay_or_flirt.search(text):
        if log:
            log("[self_agent] classify skipped: role-play / flirt message")
        return False

    # A bare "发我" in a private chat usually means "send me that picture or
    # sticker", not "operate QQ on my behalf".  Keep this deterministic guard
    # narrow so requests with an explicit target still reach the classifier.
    if re.fullmatch(
        r"\s*(?:私聊)?(?:发我|发给我|再发(?:一下)?|发一下|来一张|发张|发个)"
        r"(?:看看|看)?[。？！?!~～]*\s*",
        text,
    ):
        if log:
            log("[self_agent] classify skipped: bare send-me chat request")
        return False

    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=12, max_retries=0)
    prompt = (
        "判断下面这句话是不是在要求一个助手去实际做一件事，而不是闲聊、寒暄、"
        "情绪表达、问观点、单纯夸奖或吐槽。\n"
        "算「要做事」的例子：查状态、看日志、跑命令、找文件、改代码、验证问题；"
        "发消息到某个群、私聊某人、撤回消息、改群公告、改名片或头衔、禁言、踢人；"
        "看群成员或群列表；查聊天记录、统计数据、整理或查梗库。\n"
        "算「闲聊」的例子：问今天吃什么、说自己心情不好、问你喜欢什么、"
        "只是在评论某件事。\n"
        "以下内容一律算闲聊并回答 NO，即使语气像命令：角色扮演、调情、性暗示、"
        "性行为、亲密互动、撒娇、让对方陪聊/表演/发图。"
        "例如「去帮我舔舔」「内射爱丽丝」「想看你当小母狗」「舔耳朵」"
        "「多发点色表情」「烧一点」「帮我撸」都不是工作任务。\n"
        "只是让机器人发图、发表情、发贴纸、发语音或发给我看，也是闲聊，回答 NO。"
        "例如「私聊发我」「发一下这个表情」「再发一张」「这张呢」「别收，发给你看的」。\n"
        "只有明确要求操作文件、命令、系统、QQ 群/成员、消息记录或数据整理，"
        "而且给出可执行对象时，才回答 YES。\n"
        "只回答 YES 或 NO。\n"
    )
    if context:
        prompt += "\n【引用的上下文】\n" + context[:500]
    prompt += "\n【当前消息】\n" + text[:500]
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,  # reasoning models spend tokens thinking before the YES/NO
        )
        msg = resp.choices[0].message
        answer = (msg.content or "").strip().upper()
        if not answer:
            # Reasoning models may answer only in reasoning_content. Read the
            # verdict from its final token rather than an arbitrary tail slice,
            # which used to misread mid-sentence thoughts as a decision.
            dumped = msg.model_dump()
            tail = str(dumped.get("reasoning_content") or "").strip().upper()
            answer = ""
            for token in reversed(re.findall(r"[A-Z]+|是|否", tail)):
                if token in ("YES", "NO", "是", "否"):
                    answer = token
                    break
    except Exception as exc:  # noqa: BLE001 - never block the chat on classifier errors
        if log:
            log(f"[self_agent] classify error: {exc}")
        return False
    if log:
        log(f"[self_agent] classify raw={answer[:40]!r}")
    if answer in ("YES", "是"):
        return True
    if answer in ("NO", "否"):
        return False
    return "YES" in answer or "是" in answer


def format_for_chat(text: str) -> str:
    """Turn agent markdown into something that reads OK in a QQ message.

    Tables and headings are dropped/normalised: QQ shows them raw and they look
    like noise on a phone.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        if set(stripped) <= {"-", ":", "|", " "} and "|" in stripped:
            continue  # table separator
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 2:
                out.append(f"· {cells[0]}：{' / '.join(c for c in cells[1:] if c)}")
                continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            out.append(f"【{heading}】" if heading else "")
            continue
        if stripped.startswith(("- ", "* ")):
            out.append("· " + stripped[2:])
            continue
        if stripped.startswith("**") and stripped.endswith("**"):
            out.append(stripped.strip("*"))
            continue
        out.append(line.replace("**", ""))
    return "\n".join(out).strip()


async def run_agent(
    *,
    base_url: str,
    api_key: str,
    model: str,
    temperature: float,
    max_steps: int,
    workdir: Path,
    user_prompt: str,
    log,
) -> tuple[str, list[str]]:
    """Run the tool loop and return (answer, trace)."""
    client = AsyncOpenAI(api_key=api_key, base_url=base_url or None, timeout=180, max_retries=0)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT + f"\n工作目录：{workdir}"},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[str] = []
    answer = ""
    root = workdir.resolve()

    def inside(path: str) -> Path | None:
        target = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        return target if str(target).startswith(str(root)) else None

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
            answer = (msg.content or "").strip()
            break

        stop = False
        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if name == "run":
                result = await _run_shell(str(args.get("cmd", "")), root)
            elif name == "read":
                target = inside(str(args.get("path", "")))
                if target is None:
                    result = "拒绝：路径超出工作目录"
                elif not target.is_file():
                    result = f"不是文件：{args.get('path')}"
                else:
                    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = args.get("tail")
                    if isinstance(tail, int) and tail > 0:
                        lines = lines[-tail:]
                    result = "\n".join(lines)[:6000]
            elif name == "list":
                target = inside(str(args.get("path", ".")))
                if target is None:
                    result = "拒绝：路径超出工作目录"
                else:
                    try:
                        names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())[:200]
                        result = "\n".join(names) or "(空目录)"
                    except OSError as exc:
                        result = f"列目录失败：{exc}"
            elif name == "finish":
                answer = str(args.get("answer", "")).strip()
                result = "已记录"
                stop = True
            else:
                result = f"未知工具：{name}"

            trace.append(f"step{step + 1} {name}: {result[:120]}")
            log(f"[self_agent] {trace[-1]}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result[:6000]})
        if stop:
            break

    return answer, trace
