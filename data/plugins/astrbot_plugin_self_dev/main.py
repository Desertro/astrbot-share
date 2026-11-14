"""Self-dev: let the bot modify its own plugin code, but only behind approval.

Flow:
    #dev <plugin> <request>  -> snapshot, run a coder agent in a scratch copy,
                                show the diff, wait for approval
    #dev ok <id>             -> back up, compile, apply, hot-reload, rollback on error
    #dev no <id>             -> discard
    #dev undo <id>           -> restore the backup of an applied job
    #dev ls                  -> list jobs

Safety: admin only, plugin allow-list, path allow-list, byte-compile gate,
automatic rollback, and every job kept on disk under data/plugin_data/self_dev/jobs.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

try:  # loaded as a package by AstrBot, but keep the script-mode fallback
    from . import coder, devcore
except ImportError:  # pragma: no cover
    import coder
    import devcore

DATA_DIR = Path(__file__).resolve().parents[2]
PLUGIN_DIR = DATA_DIR / "plugins"
JOB_ROOT = DATA_DIR / "plugin_data" / "self_dev" / "jobs"
SELF_DIR = "astrbot_plugin_self_dev"
MEM_CHAT_CONF = DATA_DIR / "config" / "astrbot_plugin_mem_chat_config.json"

SYSTEM_PROMPT = """你是一个只改代码的维护工。你只能通过工具改动工作区里的文件。

规则：
1. 先 sd_list / sd_read 看清楚现状，再动手；改动尽量小，不要重构无关代码。
2. 只改必要的文件；不要新建无关文件，不要动数据库、日志、缓存、__pycache__。
3. 改完必须调用 sd_check 做语法检查，直到通过。
4. 不要臆造 API：只使用工作区里已经出现的导入和写法。
5. 全部完成后调用 sd_finish，写一句中文总结，说明改了哪个文件、改了什么。
6. 不要把完整文件内容贴给用户，diff 由系统生成。
"""


@dataclass
class DevJob:
    job_id: str
    plugin: str
    request: str
    status: str = "pending"
    created: float = 0.0
    diff: str = ""
    changed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    summary: str = ""
    note: str = ""

    @property
    def root(self) -> Path:
        return JOB_ROOT / self.job_id

    @property
    def base_dir(self) -> Path:
        return self.root / "base"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def backup_dir(self) -> Path:
        return self.root / "backup"

    def to_json(self) -> dict:
        return {
            "job_id": self.job_id,
            "plugin": self.plugin,
            "request": self.request,
            "status": self.status,
            "created": self.created,
            "diff": self.diff,
            "changed": self.changed,
            "added": self.added,
            "removed": self.removed,
            "summary": self.summary,
            "note": self.note,
        }

    def save(self) -> None:
        path = self.root / "job.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, job_id: str) -> "DevJob | None":
        path = JOB_ROOT / job_id / "job.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


@register("self_dev", "dsh", "让机器人改自己的插件代码（审批后热重载）", "0.1.0")
class SelfDev(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.conf = config or {}
        JOB_ROOT.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- utils

    def _c(self, key: str, default):
        value = self.conf.get(key, default)
        return default if value is None else value

    def _admin_uids(self) -> set[str]:
        raw = self._c("admin_uids", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        return {str(x).strip() for x in raw}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return str(event.get_sender_id()) in self._admin_uids()

    def _allowed(self) -> list[str]:
        raw = self._c("allowed_plugins", [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        return [str(x).strip() for x in raw if str(x).strip()]

    def _resolve_plugin_dir(self, name: str) -> Path | None:
        """Find a plugin directory by directory name, short name, or metadata name."""
        if name not in self._allowed():
            return None
        for candidate in PLUGIN_DIR.iterdir():
            if not candidate.is_dir():
                continue
            if candidate.name in (name, f"astrbot_plugin_{name}"):
                return candidate
        return None

    def _coder_creds(self) -> tuple[str, str, str]:
        """Return (base_url, api_key, model), falling back to mem_chat's own creds."""
        base_url = str(self._c("coder_base_url", "")).strip()
        api_key = str(self._c("coder_api_key", "")).strip()
        model = str(self._c("coder_model", "")).strip()
        if (not base_url or not api_key) and MEM_CHAT_CONF.exists():
            try:
                conf = json.loads(MEM_CHAT_CONF.read_text(encoding="utf-8-sig"))
                base_url = base_url or str(conf.get("llm_base_url", "")).strip()
                api_key = api_key or str(conf.get("llm_api_key", "")).strip()
                model = model or str(conf.get("llm_model", "")).strip()
            except Exception:  # noqa: BLE001
                pass
        return base_url, api_key, model or "deepseek-flash"

    def _jobs_summary(self) -> str:
        rows = []
        files = sorted(JOB_ROOT.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
        for job_file in files:
            data = json.loads(job_file.read_text(encoding="utf-8"))
            rows.append(
                f"{data['job_id']} [{data['status']}] {data['plugin']} "
                f"改{len(data.get('changed', []))}增{len(data.get('added', []))}删{len(data.get('removed', []))} "
                f"{(data.get('summary') or data.get('request') or '')[:24]}"
            )
        return "\n".join(rows) or "还没有任务"

    # ------------------------------------------------------------- commands

    @filter.command("dev")
    async def cmd_dev(self, event: AstrMessageEvent, args: GreedyStr):
        """#dev <plugin> <需求> / #dev ok|no|undo <id> / #dev ls"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能改代码。")
            return

        text = str(args).strip()
        if not text or text in ("help", "?"):
            yield event.plain_result(
                "用法：\n"
                "#dev <plugin> <需求>  让 agent 在副本上改，出 diff\n"
                "#dev ok <id>          备份→语法检查→热重载，失败自动回滚\n"
                "#dev no <id>          丢弃\n"
                "#dev undo <id>        撤销已应用的任务\n"
                "#dev ls               最近任务"
            )
            return

        head, _, rest = text.partition(" ")
        head = head.strip()
        rest = rest.strip()

        if head in ("ls", "list"):
            yield event.plain_result(self._jobs_summary())
            return
        if head in ("ok", "apply", "yes"):
            yield event.plain_result(await self._apply_job(rest))
            return
        if head in ("no", "drop", "cancel"):
            job = DevJob.load(rest)
            if not job:
                yield event.plain_result(f"找不到任务 {rest}")
                return
            job.status = "dropped"
            job.save()
            yield event.plain_result(f"已丢弃 {job.job_id}")
            return
        if head == "undo":
            yield event.plain_result(await self._undo_job(rest))
            return

        if not rest:
            yield event.plain_result("缺需求。用法：#dev <plugin> <需求>")
            return

        yield event.plain_result(await self._start_job(event, head, rest))

    # ------------------------------------------------------------- job flow

    async def _start_job(self, event: AstrMessageEvent, plugin: str, request: str) -> str:
        plugin_dir = self._resolve_plugin_dir(plugin)
        if not plugin_dir:
            allowed = ", ".join(self._allowed()) or "空"
            return f"插件 {plugin} 不在白名单里（allowed_plugins）：{allowed}"
        if plugin_dir.name == SELF_DIR:
            return "不能改自己。"

        job = DevJob(
            job_id=time.strftime("%m%d_%H%M%S"),
            plugin=plugin_dir.name,
            request=request,
            created=time.time(),
        )
        try:
            devcore.snapshot(plugin_dir, job.base_dir)
            devcore.snapshot(plugin_dir, job.work_dir)
        except Exception as exc:  # noqa: BLE001
            return f"准备副本失败：{exc}"

        ok, err = devcore.compile_tree(job.work_dir)
        if not ok:
            return f"原插件本身就编译不过，先修好再来：\n{err[:600]}"

        base_url, api_key, model = self._coder_creds()
        if not api_key:
            return "没有可用的模型凭据：请在插件配置里填 coder_api_key/base_url/model。"

        self.logger.info(f"[self_dev] job {job.job_id} start plugin={plugin} req={request[:60]}")
        system_prompt = SYSTEM_PROMPT + f"\n工作区根目录：{job.work_dir}\n目标插件：{plugin_dir.name}\n"

        try:
            summary, trace = await coder.run_coder(
                base_url=base_url,
                api_key=api_key,
                model=model,
                temperature=float(self._c("coder_temperature", 0.2)),
                max_steps=int(self._c("max_steps", 16)),
                system_prompt=system_prompt,
                user_prompt=f"需求：{request}",
                dispatch=lambda name, kwargs: self._dispatch(job, name, kwargs),
                log=self.logger.info,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"[self_dev] coder failed: {exc}")
            return f"编码 agent 执行失败：{exc}"

        job.summary = (summary or "")[:400]
        diff, changed, added, removed = devcore.diff_trees(
            job.base_dir, job.work_dir, int(self._c("max_diff_chars", 12000))
        )
        job.diff, job.changed, job.added, job.removed = diff, changed, added, removed

        if not (changed or added or removed):
            return f"agent 没有改动任何文件。\n步骤：\n" + "\n".join(trace[-5:])

        job.status = "pending"
        job.save()
        (job.root / "diff.txt").write_text(diff, encoding="utf-8")

        files = ", ".join([*changed, *(f"+{x}" for x in added), *(f"-{x}" for x in removed)])
        head = f"任务 {job.job_id}（{plugin}）\n改动：{files}\n"
        if job.summary:
            head += f"说明：{job.summary}\n"
        body = diff[: max(600, int(self._c("max_diff_in_chat", 1500)))]
        return head + "\n" + body + f"\n\n回复「#dev ok {job.job_id}」应用；「#dev no {job.job_id}」丢弃。"

    async def _apply_job(self, job_id: str) -> str:
        job = DevJob.load(job_id)
        if not job:
            return f"找不到任务 {job_id}"
        if job.status == "applied":
            return f"{job_id} 已经应用过了"
        plugin_dir = PLUGIN_DIR / job.plugin
        if not plugin_dir.exists():
            return f"目标目录不存在：{plugin_dir}"

        touched = job.changed + job.added
        if not touched:
            return "没有需要应用的文件"

        try:
            devcore.backup_files(plugin_dir, job.backup_dir, job.changed)
            devcore.copy_into(job.work_dir, plugin_dir, touched)
        except Exception as exc:  # noqa: BLE001
            return f"写入失败（未改动线上）：{exc}"

        ok, err = devcore.compile_tree(plugin_dir)
        if not ok:
            devcore.restore_files(plugin_dir, job.backup_dir, job.changed, job.added)
            job.status, job.note = "failed", f"compile: {err[:300]}"
            job.save()
            return f"语法检查失败，已回滚：\n{err[:600]}"

        reload_ok, reload_err = await self._reload(job.plugin)
        if not reload_ok:
            devcore.restore_files(plugin_dir, job.backup_dir, job.changed, job.added)
            await self._reload(job.plugin)
            job.status, job.note = "failed", f"reload: {reload_err}"
            job.save()
            return f"热重载失败，已回滚：{reload_err}"

        job.status = "applied"
        job.note = f"applied at {time.strftime('%H:%M:%S')}"
        job.save()
        return (
            f"{job_id} 已应用并重载 {job.plugin}。\n"
            f"改动文件：{', '.join(touched)}\n"
            f"要撤销：「#dev undo {job_id}」"
        )

    async def _undo_job(self, job_id: str) -> str:
        job = DevJob.load(job_id)
        if not job or job.status != "applied":
            return f"{job_id} 不是已应用状态，没法撤销"
        plugin_dir = PLUGIN_DIR / job.plugin
        devcore.restore_files(plugin_dir, job.backup_dir, job.changed, job.added)
        ok, _ = devcore.compile_tree(plugin_dir)
        reload_ok, reload_err = await self._reload(job.plugin)
        job.status = "undone"
        job.save()
        return f"{job_id} 已撤销。编译={'通过' if ok else '失败'} 重载={'成功' if reload_ok else reload_err}"

    async def _reload(self, plugin_name: str) -> tuple[bool, str]:
        manager = getattr(self.context, "_star_manager", None)
        if not manager:
            return False, "拿不到 plugin manager"
        try:
            result = await manager.reload(plugin_name)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        if isinstance(result, tuple):
            success, err = (result + (None, None))[:2]
            return bool(success), str(err or "")
        return bool(result), ""

    # ---------------------------------------------------------------- tools

    async def _dispatch(self, job: DevJob, name: str, args: dict) -> str:
        if name == "sd_list":
            root = job.work_dir / args["path"] if args.get("path") else job.work_dir
            if not str(root.resolve()).startswith(str(job.work_dir.resolve())):
                return "拒绝：路径越界"
            return devcore.list_tree(root)

        if name == "sd_read":
            path = str(args.get("path", ""))
            if not devcore.is_editable(path):
                return "拒绝：只允许读写代码/文本文件"
            target = (job.work_dir / path).resolve()
            if not str(target).startswith(str(job.work_dir.resolve())):
                return "拒绝：路径越界"
            if not target.exists():
                return f"文件不存在：{path}"
            text = devcore.read_text(target)
            return text[:16000] + (f"\n... (截断，共 {len(text)} 字符)" if len(text) > 16000 else "")

        if name == "sd_write":
            path = str(args.get("path", ""))
            content = str(args.get("content", ""))
            if not devcore.is_editable(path):
                return "拒绝：只允许 .py/.json/.md 等文本文件"
            target = (job.work_dir / path).resolve()
            if not str(target).startswith(str(job.work_dir.resolve())):
                return "拒绝：路径越界"
            if len(content) > 200000:
                return "拒绝：单文件太大"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"已写入 {path}（{len(content)} 字符）"

        if name == "sd_check":
            ok, output = devcore.compile_tree(job.work_dir)
            return ("语法检查通过" if ok else "语法检查失败") + f"\n{output}"

        if name == "sd_finish":
            job.summary = str(args.get("summary", ""))[:400]
            return "已记录总结，结束。"

        return f"未知工具：{name}"

    async def terminate(self):
        pass
