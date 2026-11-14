import asyncio
import json
import os
import random
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


def _now_ts() -> float:
    return time.time()


def _safe_int(v: Any, d: int) -> int:
    try:
        return int(v)
    except Exception:
        return int(d)


@register("mem_scheduler", "desertro", "mem_chat 定时任务编排插件", "0.1.0", "")
class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        tz = str(self.config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        self.scheduler = AsyncIOScheduler(timezone=tz)

        data_root = os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_mem_scheduler")
        os.makedirs(data_root, exist_ok=True)
        self.jobs_file = os.path.join(data_root, "jobs.json")
        self.state_file = os.path.join(data_root, "state.json")

        self.jobs: dict[str, dict] = {}
        self.state: dict[str, Any] = {
            "history": [],
            "nickname_state": {},
            "job_runtime": {},
            "dry_run": bool(self.config.get("default_dry_run", True)),
        }
        self._job_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._invoke_ts: list[float] = []
        self._group_runtime: dict[str, dict] = {}

        self._load_state()
        self._ensure_state_containers()
        self._load_jobs()
        self._ensure_seed_jobs()
        self._init_scheduler()
        self.scheduler.start()
        logger.info("[mem_scheduler] plugin initialized.")

    async def terminate(self):
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        self._save_jobs()
        self._save_state()
        logger.info("[mem_scheduler] plugin terminated.")

    async def on_message(self, event: AstrMessageEvent):
        self._capture_runtime_from_event(event)

    def _capture_runtime_from_event(self, event: AstrMessageEvent):
        gid = event.get_group_id()
        if not gid:
            return
        try:
            self._group_runtime[str(gid)] = {
                "bot": event.bot,
                "self_id": str(event.get_self_id() or ""),
                "platform": str(event.get_platform_name() or ""),
                "updated_at": _now_ts(),
            }
        except Exception:
            pass

    def _ensure_seed_jobs(self):
        seeds = self.config.get("seed_jobs", [])
        if not isinstance(seeds, list):
            return
        changed = False
        for it in seeds:
            if not isinstance(it, dict):
                continue
            jid = str(it.get("id") or "").strip()
            if not jid or jid in self.jobs:
                continue
            job = self._normalize_job(it, creator="seed")
            if job:
                self.jobs[job["id"]] = job
                changed = True
        if changed:
            self._save_jobs()

    def _load_jobs(self):
        if not os.path.exists(self.jobs_file):
            self.jobs = {}
            self._save_jobs()
            return
        try:
            with open(self.jobs_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                parsed = {}
                for it in data:
                    job = self._normalize_job(it, creator="import")
                    if job:
                        parsed[job["id"]] = job
                self.jobs = parsed
            else:
                self.jobs = {}
        except Exception as e:
            logger.warning(f"[mem_scheduler] load jobs failed: {e}")
            self.jobs = {}

    def _save_jobs(self):
        try:
            rows = sorted(self.jobs.values(), key=lambda x: str(x.get("id", "")))
            with open(self.jobs_file, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[mem_scheduler] save jobs failed: {e}")

    def _load_state(self):
        if not os.path.exists(self.state_file):
            self._save_state()
            return
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state.update(data)
        except Exception as e:
            logger.warning(f"[mem_scheduler] load state failed: {e}")

    def _ensure_state_containers(self):
        if not isinstance(self.state.get("history"), list):
            self.state["history"] = []
        if not isinstance(self.state.get("nickname_state"), dict):
            self.state["nickname_state"] = {}
        if not isinstance(self.state.get("job_runtime"), dict):
            self.state["job_runtime"] = {}
        if "dry_run" not in self.state:
            self.state["dry_run"] = bool(self.config.get("default_dry_run", True))

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[mem_scheduler] save state failed: {e}")

    def _append_history(self, row: dict):
        history = self.state.get("history")
        if not isinstance(history, list):
            history = []
            self.state["history"] = history
        history.append(row)
        if len(history) > 200:
            self.state["history"] = history[-200:]
        self._save_state()

    def _job_result(
        self,
        success: bool,
        outcome: str,
        error: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "success": bool(success),
            "outcome": str(outcome or ("ok" if success else "failed")),
            "error": str(error or ""),
        }
        for key, value in extra.items():
            if value is not None:
                row[key] = value
        return row

    def _parse_job_result(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return self._job_result(
                bool(result.get("success", False)),
                str(result.get("outcome") or ("ok" if result.get("success") else "failed")),
                str(result.get("error") or ""),
                **{k: v for k, v in result.items() if k not in {"success", "outcome", "error"}},
            )
        return self._job_result(bool(result), "ok" if result else "failed")

    def _get_job_runtime(self, job_id: str) -> dict[str, Any]:
        runtime_map = self.state.get("job_runtime")
        if not isinstance(runtime_map, dict):
            runtime_map = {}
            self.state["job_runtime"] = runtime_map
        rec = runtime_map.get(job_id)
        if not isinstance(rec, dict):
            rec = {
                "success_count": 0,
                "last_success_ts": 0,
                "last_card": "",
                "last_reason": "",
            }
            runtime_map[job_id] = rec
        return rec

    def _mark_job_success(self, job_id: str, card: str = "", reason: str = "") -> dict[str, Any]:
        rec = self._get_job_runtime(job_id)
        rec["success_count"] = _safe_int(rec.get("success_count"), 0) + 1
        rec["last_success_ts"] = int(_now_ts())
        if card:
            rec["last_card"] = str(card)
        if reason:
            rec["last_reason"] = str(reason)
        runtime_map = self.state.get("job_runtime")
        if isinstance(runtime_map, dict):
            runtime_map[job_id] = rec
        self._save_state()
        return rec

    def _disable_job(self, job_id: str, reason: str = "") -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        job["enabled"] = False
        try:
            job["meta"]["updated_at"] = int(_now_ts())
        except Exception:
            pass
        if reason:
            try:
                job["meta"]["disabled_reason"] = str(reason)
            except Exception:
                pass
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        self._save_jobs()
        return True

    def _render_force_card(self, template: str, job_id: str, run_index: int, max_runs: int) -> str:
        text = str(template or "")
        return (
            text.replace("{run}", str(run_index))
            .replace("{count}", str(run_index))
            .replace("{max_runs}", str(max_runs if max_runs > 0 else ""))
            .replace("{job_id}", str(job_id))
        ).strip()

    def _parse_hhmm(self, v: str, default_min: int) -> int:
        s = str(v or "").strip()
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", s)
        if not m:
            return int(default_min)
        hh = max(0, min(23, _safe_int(m.group(1), 0)))
        mm = max(0, min(59, _safe_int(m.group(2), 0)))
        return hh * 60 + mm

    def _current_week_key(self) -> str:
        now = datetime.now()
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"

    def _random_daily_should_run(self, job: dict) -> bool:
        payload = job.get("payload", {})
        if not isinstance(payload, dict) or not bool(payload.get("random_daily_once", False)):
            return True

        state = self.state.get("random_daily_state")
        if not isinstance(state, dict):
            state = {}
            self.state["random_daily_state"] = state

        jid = str(job.get("id") or "")
        if not jid:
            return True

        start_m = self._parse_hhmm(str(payload.get("window_start") or "10:00"), 10 * 60)
        end_m = self._parse_hhmm(str(payload.get("window_end") or "23:00"), 23 * 60)
        if end_m < start_m:
            start_m, end_m = end_m, start_m
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        cur_m = now.hour * 60 + now.minute

        rec = state.get(jid, {})
        if not isinstance(rec, dict) or str(rec.get("day") or "") != today:
            target_m = random.randint(start_m, end_m)
            rec = {"day": today, "target_minute": target_m, "done": False}
            state[jid] = rec
            self._save_state()

        if bool(rec.get("done", False)):
            return False
        target_m = _safe_int(rec.get("target_minute"), start_m)
        return cur_m >= target_m

    def _random_weekly_should_run(self, job: dict) -> bool:
        payload = job.get("payload", {})
        if not isinstance(payload, dict) or not bool(payload.get("random_weekly_once", False)):
            return True

        state = self.state.get("random_weekly_state")
        if not isinstance(state, dict):
            state = {}
            self.state["random_weekly_state"] = state

        jid = str(job.get("id") or "")
        if not jid:
            return True

        start_m = self._parse_hhmm(str(payload.get("window_start") or "10:00"), 10 * 60)
        end_m = self._parse_hhmm(str(payload.get("window_end") or "23:00"), 23 * 60)
        if end_m < start_m:
            start_m, end_m = end_m, start_m

        weekdays_raw = payload.get("weekdays")
        weekdays: list[int] = []
        if isinstance(weekdays_raw, list):
            for item in weekdays_raw:
                val = _safe_int(item, -1)
                if 0 <= val <= 6:
                    weekdays.append(val)
        if not weekdays:
            weekdays = list(range(7))

        now = datetime.now()
        week_key = self._current_week_key()
        cur_day = now.weekday()
        cur_m = now.hour * 60 + now.minute

        rec = state.get(jid, {})
        if not isinstance(rec, dict) or str(rec.get("week") or "") != week_key:
            target_day = random.choice(weekdays)
            target_m = random.randint(start_m, end_m)
            rec = {
                "week": week_key,
                "target_weekday": target_day,
                "target_minute": target_m,
                "done": False,
            }
            state[jid] = rec
            self._save_state()

        if bool(rec.get("done", False)):
            return False
        target_day = _safe_int(rec.get("target_weekday"), weekdays[0])
        target_m = _safe_int(rec.get("target_minute"), start_m)
        if cur_day < target_day:
            return False
        if cur_day > target_day:
            return True
        return cur_m >= target_m

    def _random_daily_mark_done(self, job: dict):
        payload = job.get("payload", {})
        if not isinstance(payload, dict) or not bool(payload.get("random_daily_once", False)):
            return
        jid = str(job.get("id") or "")
        if not jid:
            return
        state = self.state.get("random_daily_state")
        if not isinstance(state, dict):
            state = {}
            self.state["random_daily_state"] = state
        rec = state.get(jid, {})
        if not isinstance(rec, dict):
            rec = {}
        rec["done"] = True
        state[jid] = rec
        self._save_state()

    def _random_weekly_mark_done(self, job: dict):
        payload = job.get("payload", {})
        if not isinstance(payload, dict) or not bool(payload.get("random_weekly_once", False)):
            return
        jid = str(job.get("id") or "")
        if not jid:
            return
        state = self.state.get("random_weekly_state")
        if not isinstance(state, dict):
            state = {}
            self.state["random_weekly_state"] = state
        rec = state.get(jid, {})
        if not isinstance(rec, dict):
            rec = {}
        rec["done"] = True
        state[jid] = rec
        self._save_state()

    def _normalize_job(self, raw: dict, creator: str) -> dict | None:
        if not isinstance(raw, dict):
            return None
        jtype = str(raw.get("type") or "").strip()
        if jtype not in {"reminder", "nickname_refresh"}:
            return None
        trigger = raw.get("trigger")
        if not isinstance(trigger, dict):
            return None
        kind = str(trigger.get("kind") or "").strip()
        if kind not in {"cron", "interval"}:
            return None
        if kind == "cron":
            expr = str(trigger.get("expr") or "").strip()
            if len(expr.split()) != 5:
                return None
        else:
            sec = _safe_int(trigger.get("seconds"), 0)
            if sec <= 0:
                return None
            min_interval = max(1, _safe_int(self.config.get("min_interval_sec", 1), 1))
            if sec < min_interval:
                return None

        jid = str(raw.get("id") or "").strip() or f"job-{uuid.uuid4().hex[:10]}"
        target_umo = str(raw.get("target_umo") or "").strip()
        if not self._is_valid_target_umo(target_umo):
            return None
        if kind == "interval" and jtype == "nickname_refresh":
            sec = _safe_int(trigger.get("seconds"), 0)
            min_nick_interval = max(
                30, _safe_int(self.config.get("nickname_min_interval_sec", 60), 60)
            )
            if sec < min_nick_interval:
                return None

        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        options = raw.get("options")
        if not isinstance(options, dict):
            options = {}
        dry_run = bool(options.get("dry_run", self.state.get("dry_run", True)))

        now = int(_now_ts())
        meta = raw.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        created_at = _safe_int(meta.get("created_at"), now)
        updated_at = _safe_int(meta.get("updated_at"), now)
        creator_val = str(meta.get("creator") or creator or "system")
        return {
            "id": jid,
            "type": jtype,
            "enabled": bool(raw.get("enabled", True)),
            "target_umo": target_umo,
            "trigger": trigger,
            "payload": payload,
            "options": {"dry_run": dry_run},
            "meta": {
                "creator": creator_val,
                "created_at": created_at,
                "updated_at": updated_at,
            },
        }

    def _is_valid_target_umo(self, target_umo: str) -> bool:
        s = str(target_umo or "").strip()
        if not s:
            return False
        parts = s.split(":")
        if len(parts) < 3:
            return False
        p0, p1, p2 = str(parts[0]).strip(), str(parts[1]).strip(), str(parts[2]).strip()
        if not p0 or not p1 or not p2:
            return False
        return True

    def _init_scheduler(self):
        for job_id, job in self.jobs.items():
            if not bool(job.get("enabled", True)):
                continue
            self._schedule_job(job_id, job)

    def _schedule_job(self, job_id: str, job: dict):
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        trigger = job.get("trigger", {})
        kind = str(trigger.get("kind") or "")
        try:
            if kind == "cron":
                fields = self._parse_cron(str(trigger.get("expr") or ""))
                self.scheduler.add_job(
                    self._run_job,
                    trigger="cron",
                    id=job_id,
                    args=[job_id, False],
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=90,
                    **fields,
                )
            elif kind == "interval":
                sec = max(1, _safe_int(trigger.get("seconds"), 60))
                self.scheduler.add_job(
                    self._run_job,
                    trigger="interval",
                    seconds=sec,
                    id=job_id,
                    args=[job_id, False],
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=90,
                )
        except Exception as e:
            logger.warning(f"[mem_scheduler] schedule job failed: {job_id}, err={e}")

    def _parse_cron(self, expr: str) -> dict:
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError("cron expression must have 5 fields.")
        return {
            "minute": parts[0],
            "hour": parts[1],
            "day": parts[2],
            "month": parts[3],
            "day_of_week": parts[4],
        }

    def _can_invoke(self) -> bool:
        max_calls = _safe_int(self.config.get("max_calls_per_minute", 12), 12)
        if max_calls <= 0:
            return True
        now = _now_ts()
        self._invoke_ts = [x for x in self._invoke_ts if now - x < 60.0]
        if len(self._invoke_ts) >= max_calls:
            return False
        self._invoke_ts.append(now)
        return True

    async def _run_job(self, job_id: str, manual: bool = False) -> bool:
        job = self.jobs.get(job_id)
        if not job:
            return False
        if not bool(job.get("enabled", True)) and not manual:
            return False
        if not self._can_invoke():
            self._append_history(
                {"ts": int(_now_ts()), "job_id": job_id, "status": "rate_limited"}
            )
            return False

        lock = self._job_locks[job_id]
        async with lock:
            started = _now_ts()
            ok = False
            err = ""
            try:
                jtype = str(job.get("type") or "")
                if jtype == "reminder":
                    result = await self._run_reminder_job(job)
                elif jtype == "nickname_refresh":
                    result = await self._run_nickname_refresh_job(job, manual=manual)
                else:
                    result = self._job_result(False, "failed", f"unsupported job type: {jtype}")
            except Exception as e:
                result = self._job_result(False, "failed", str(e))

            parsed = self._parse_job_result(locals().get("result"))
            ok = bool(parsed.get("success", False))
            err = str(parsed.get("error") or "")
            outcome = str(parsed.get("outcome") or ("ok" if ok else "failed"))

            if ok and not str(outcome).startswith("skipped"):
                self._random_daily_mark_done(job)
                self._random_weekly_mark_done(job)
                # one-shot reminder: disable after first successful delivery
                if str(job.get("type") or "") == "reminder":
                    payload = job.get("payload", {})
                    if isinstance(payload, dict) and bool(payload.get("once", False)):
                        job["enabled"] = False
                        try:
                            job["meta"]["updated_at"] = int(_now_ts())
                        except Exception:
                            pass
                        try:
                            self.scheduler.remove_job(job_id)
                        except Exception:
                            pass
                        self._save_jobs()

            self._append_history(
                {
                    "ts": int(_now_ts()),
                    "job_id": job_id,
                    "type": str(job.get("type") or ""),
                    "manual": bool(manual),
                    "status": "ok" if ok else "failed",
                    "outcome": outcome,
                    "cost_ms": int((_now_ts() - started) * 1000),
                    "error": err[:500],
                    "details": {
                        k: v
                        for k, v in parsed.items()
                        if k not in {"success", "outcome", "error"}
                    },
                }
            )
            return ok

    def _get_mem_chat(self):
        try:
            meta = self.context.get_registered_star("mem_chat")
            return meta.star_cls if meta else None
        except Exception:
            return None

    async def _send_text(self, target_umo: str, text: str) -> bool:
        if not target_umo or not text:
            return False
        try:
            await self.context.send_message(
                target_umo, MessageEventResult().message(str(text))
            )
            return True
        except Exception as e:
            logger.warning(f"[mem_scheduler] send message failed: {e}")
            return False

    def _build_at_prefix(self, target_umo: str, qq: str) -> str:
        if not qq:
            return ""
        kind = str(target_umo or "").split(":")[1].lower() if ":" in str(target_umo or "") else ""
        if kind in {"group", "groupmessage"}:
            return f"[CQ:at,qq={qq}] "
        return ""

    async def _run_reminder_job(self, job: dict) -> bool:
        target_umo = str(job.get("target_umo") or "")
        payload = job.get("payload", {})
        text = str(payload.get("text") or "定时提醒：请处理计划事项。").strip()
        mention_qq = str(payload.get("mention_qq") or "").strip()
        mention_name = str(payload.get("mention_name") or "").strip()
        mem_chat = self._get_mem_chat()
        if mem_chat and hasattr(mem_chat, "submit_reminder_task"):
            try:
                return self._job_result(
                    bool(
                    await mem_chat.submit_reminder_task(
                        target_umo=target_umo,
                        text=text,
                        mention_qq=mention_qq,
                        mention_name=mention_name,
                    )
                    ),
                    "delegated" if mem_chat else "sent",
                )
            except Exception:
                pass
        if mem_chat and hasattr(mem_chat, "share_text_to_session"):
            try:
                return self._job_result(
                    bool(
                    await mem_chat.share_text_to_session(
                        target_umo,
                        self._build_at_prefix(target_umo, mention_qq) + text,
                    )
                    ),
                    "shared_to_session",
                )
            except Exception:
                pass
        ok = await self._send_text(target_umo, self._build_at_prefix(target_umo, mention_qq) + text)
        return self._job_result(ok, "sent" if ok else "send_failed")

    def _extract_group_id(self, target_umo: str) -> str:
        parts = str(target_umo or "").split(":")
        if len(parts) < 3:
            return ""
        kind = str(parts[1] or "").strip().lower()
        if kind in {"group", "groupmessage"}:
            return str(parts[2] or "")
        return ""

    def _extract_platform_id(self, target_umo: str) -> str:
        parts = str(target_umo or "").split(":")
        if len(parts) < 1:
            return ""
        return str(parts[0] or "").strip()

    async def _resolve_bot_and_self_id(
        self,
        gid: str,
        target_umo: str,
        self_id_override: str = "",
    ) -> tuple[Any | None, str]:
        runtime = self._group_runtime.get(str(gid))
        if runtime:
            bot = runtime.get("bot")
            self_id = str(runtime.get("self_id") or "")
            if bot:
                if self_id_override:
                    return bot, str(self_id_override)
                if self_id:
                    return bot, self_id

        # fallback: infer from mem_chat state if available
        inferred_self_id = str(self_id_override or "").strip()
        try:
            mem_chat = self._get_mem_chat()
            if mem_chat and hasattr(mem_chat, "state") and hasattr(mem_chat.state, "get_self_id"):
                if not inferred_self_id:
                    inferred_self_id = str(mem_chat.state.get_self_id(target_umo) or "").strip()
        except Exception:
            inferred_self_id = ""

        platform_hint = self._extract_platform_id(target_umo)
        insts = []
        try:
            insts = list(self.context.platform_manager.get_insts() or [])
        except Exception:
            insts = []

        # Prefer platform id matched adapter first.
        insts.sort(
            key=lambda x: 0
            if str(getattr(getattr(x, "meta", lambda: None)(), "id", "") or "") == platform_hint
            else 1
        )

        for inst in insts:
            bot = getattr(inst, "bot", None)
            if not bot:
                continue
            try:
                if inferred_self_id:
                    return bot, inferred_self_id
                info = await bot.get_login_info()
                data = info.get("data", {}) if isinstance(info, dict) else {}
                sid = str(data.get("user_id") or "").strip()
                if sid:
                    return bot, sid
            except Exception:
                continue
        return None, ""

    def _extract_json_obj(self, text: str) -> dict | None:
        s = str(text or "").strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
        return None

    def _nickname_allow(self, target_umo: str) -> tuple[bool, str]:
        state = self.state.get("nickname_state")
        if not isinstance(state, dict):
            state = {}
            self.state["nickname_state"] = state
        now = int(_now_ts())
        today = datetime.now().strftime("%Y-%m-%d")
        rec = state.get(target_umo, {})
        if not isinstance(rec, dict):
            rec = {}
        day = str(rec.get("day") or "")
        day_count = _safe_int(rec.get("day_count"), 0)
        last_ts = _safe_int(rec.get("last_ts"), 0)
        cooldown = _safe_int(self.config.get("nickname_cooldown_sec", 21600), 21600)
        daily_limit = _safe_int(self.config.get("nickname_daily_limit", 2), 2)

        if day != today:
            day_count = 0
            day = today
        if now - last_ts < max(0, cooldown):
            return False, f"cooldown({cooldown}s)"
        if day_count >= max(0, daily_limit):
            return False, f"daily_limit({daily_limit})"
        return True, "ok"

    def _nickname_commit(self, target_umo: str, card: str):
        state = self.state.get("nickname_state")
        if not isinstance(state, dict):
            state = {}
            self.state["nickname_state"] = state
        today = datetime.now().strftime("%Y-%m-%d")
        now = int(_now_ts())
        rec = state.get(target_umo, {})
        if not isinstance(rec, dict):
            rec = {}
        day = str(rec.get("day") or "")
        day_count = _safe_int(rec.get("day_count"), 0)
        if day != today:
            day_count = 0
        rec["day"] = today
        rec["day_count"] = day_count + 1
        rec["last_ts"] = now
        rec["last_card"] = str(card or "")
        state[target_umo] = rec
        self._save_state()

    async def _llm_decide_nickname(self, target_umo: str, topic: str = "") -> dict:
        provider_id = str(self.config.get("llm_provider_id") or "").strip()
        if not provider_id:
            provider = self.context.get_using_provider()
            provider_id = str(getattr(provider, "id", "") or "").strip()
        if not provider_id:
            return {"should_change": False, "reason": "no_provider"}

        max_len = _safe_int(self.config.get("nickname_max_len", 18), 18)
        prompt = (
            "你是群聊机器人的昵称策略助手。请只输出JSON，不要解释。\n"
            f"目标会话: {target_umo}\n"
            f"最大昵称长度: {max_len}\n"
            f"主题偏好: {topic or '无'}\n"
            "输出格式: {\"should_change\":bool,\"new_card\":\"...\",\"reason\":\"...\"}\n"
            "规则:\n"
            "1) 如果没必要修改, should_change=false。\n"
            "2) new_card 必须简短、自然、可读，不要符号堆砌。\n"
            "3) 若 should_change=true，new_card 不能为空。\n"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是严格输出JSON的调度策略模型。",
            )
            obj = self._extract_json_obj(getattr(resp, "completion_text", ""))
            if isinstance(obj, dict):
                return obj
        except Exception as e:
            logger.warning(f"[mem_scheduler] llm decide nickname failed: {e}")
        return {"should_change": False, "reason": "llm_failed"}

    async def _decide_nickname(self, target_umo: str, topic: str = "") -> dict:
        mem_chat = self._get_mem_chat()
        max_len = _safe_int(self.config.get("nickname_max_len", 18), 18)
        if mem_chat and hasattr(mem_chat, "suggest_self_group_card"):
            if hasattr(mem_chat, "is_group_enabled_for_target"):
                try:
                    enabled, reason = mem_chat.is_group_enabled_for_target(target_umo)
                    if not enabled:
                        logger.warning(
                            f"[mem_scheduler] skip mem_chat nickname decide for {target_umo}: {reason}"
                        )
                        return {"should_change": False, "reason": reason}
                except Exception as e:
                    logger.warning(f"[mem_scheduler] mem_chat group check failed: {e}")
            try:
                result = await mem_chat.suggest_self_group_card(
                    target_umo=target_umo,
                    topic=topic,
                    max_len=max_len,
                )
                if isinstance(result, dict):
                    return result
            except Exception as e:
                logger.warning(f"[mem_scheduler] mem_chat nickname decide failed: {e}")
        return await self._llm_decide_nickname(target_umo, topic=topic)

    def _sanitize_card(self, card: str) -> tuple[bool, str]:
        text = str(card or "").strip()
        if not text:
            return False, ""
        max_len = _safe_int(self.config.get("nickname_max_len", 18), 18)
        if len(text) > max_len:
            text = text[:max_len].strip()
        forbidden = self.config.get("forbidden_words", [])
        if isinstance(forbidden, list):
            low = text.lower()
            for w in forbidden:
                if str(w or "").strip().lower() in low:
                    return False, ""
        return True, text

    async def _run_nickname_refresh_job(self, job: dict, manual: bool = False) -> dict[str, Any]:
        job_id = str(job.get("id") or "")
        if not manual and not self._random_daily_should_run(job):
            return self._job_result(True, "skipped_random_window")
        if not manual and not self._random_weekly_should_run(job):
            return self._job_result(True, "skipped_random_week_window")
        target_umo = str(job.get("target_umo") or "")
        gid = self._extract_group_id(target_umo)
        if not gid:
            return self._job_result(False, "failed", "invalid_group")
        payload = job.get("payload", {})
        force_direct_set = bool(payload.get("force_direct_set", False))
        force_card = str(payload.get("force_card") or "").strip()
        force_card_template = str(payload.get("force_card_template") or "").strip()
        max_success_runs = max(
            0,
            _safe_int(
                payload.get("max_success_runs", payload.get("max_runs", 0)),
                0,
            ),
        )
        bypass_limits = bool(payload.get("bypass_limits", False))
        notify_every_success = bool(payload.get("notify_every_success", True))
        accept_unverified_success = bool(payload.get("accept_unverified_success", False))
        runtime_rec = self._get_job_runtime(job_id)
        success_count = _safe_int(runtime_rec.get("success_count"), 0)
        next_run_index = success_count + 1
        if max_success_runs > 0 and success_count >= max_success_runs:
            self._disable_job(job_id, reason="max_success_runs_reached")
            return self._job_result(
                True,
                "already_completed",
                completed_runs=success_count,
            )
        if force_card_template and not force_card:
            force_card = self._render_force_card(
                force_card_template,
                job_id=job_id,
                run_index=next_run_index,
                max_runs=max_success_runs,
            )
        force_mode = force_direct_set or bool(force_card)
        mem_chat_only = bool(self.config.get("nickname_mem_chat_only", True))
        if not manual:
            if not force_mode and not bypass_limits:
                allow, reason = self._nickname_allow(target_umo)
                if not allow:
                    # Auto schedule should stay quiet on cooldown/day-limit to avoid group spam.
                    if bool(self.config.get("notify_skip_in_group", False)):
                        await self._send_text(target_umo, f"[昵称刷新] 本轮跳过：{reason}")
                    return self._job_result(True, f"skipped_{reason}", reason)

        topic = str(payload.get("topic") or "").strip()
        force_self_id = str(payload.get("force_self_id") or "").strip()
        must_change = bool(payload.get("must_change", False))
        mem_chat = self._get_mem_chat()
        max_len = _safe_int(self.config.get("nickname_max_len", 18), 18)
        if (not force_mode) and mem_chat and hasattr(mem_chat, "submit_nickname_refresh_task"):
            if hasattr(mem_chat, "is_group_enabled_for_target"):
                try:
                    enabled, reason = mem_chat.is_group_enabled_for_target(target_umo)
                    if not enabled:
                        logger.warning(
                            f"[mem_scheduler] nickname_refresh target not enabled in mem_chat: {target_umo}, reason={reason}"
                        )
                        if mem_chat_only:
                            return self._job_result(False, "failed", reason)
                except Exception as e:
                    logger.warning(f"[mem_scheduler] mem_chat group check failed: {e}")
            try:
                topic_for_run = topic
                if must_change and max_success_runs > 0:
                    last_card = str(runtime_rec.get("last_card") or "").strip() or "无"
                    if topic_for_run:
                        topic_for_run += "；"
                    topic_for_run += (
                        f"当前是第{next_run_index}/{max_success_runs}次链路校验，请改成一个与你上次不同的新群名片。"
                        f"上次名片：{last_card}"
                    )
                bot, sid = await self._resolve_bot_and_self_id(gid, target_umo, self_id_override=force_self_id)
                runtime = {}
                if bot:
                    runtime["bot"] = bot
                if sid:
                    runtime["self_id"] = sid
                delegated = await mem_chat.submit_nickname_refresh_task(
                    target_umo=target_umo,
                    topic=topic_for_run,
                    max_len=max_len,
                    runtime=runtime,
                    must_change=must_change,
                )
                if isinstance(delegated, dict):
                    parsed = self._parse_job_result(delegated)
                    if parsed.get("success"):
                        outcome = str(parsed.get("outcome") or "")
                        card = str(parsed.get("card") or "").strip()
                        if outcome == "updated_via_mem_chat" and card:
                            runtime_rec = self._mark_job_success(
                                job_id, card=card, reason=str(parsed.get("reason") or "")
                            )
                            success_count = _safe_int(runtime_rec.get("success_count"), 0)
                            if max_success_runs > 0:
                                if notify_every_success:
                                    await self._send_text(
                                        target_umo,
                                        f"[昵称刷新测试] 第{success_count}/{max_success_runs}次已通过 mem_chat 更新群名片：{card}",
                                    )
                                if success_count >= max_success_runs:
                                    self._disable_job(job_id, reason="max_success_runs_reached")
                                    await self._send_text(
                                        target_umo,
                                        f"[昵称刷新测试] 已完成 {success_count} 次 mem_chat 改名验证，任务已自动停用。",
                                    )
                                    return self._job_result(
                                        True,
                                        "updated_via_mem_chat_and_completed",
                                        card=card,
                                        completed_runs=success_count,
                                    )
                                return self._job_result(
                                    True,
                                    "updated_via_mem_chat",
                                    card=card,
                                    completed_runs=success_count,
                                )
                            return self._job_result(True, "updated_via_mem_chat", card=card)
                        if "outcome" not in parsed or outcome == "ok":
                            parsed["outcome"] = "delegated_unverified"
                        return parsed
                elif delegated:
                    return self._job_result(True, "delegated_unverified")
            except Exception as e:
                logger.warning(f"[mem_scheduler] submit nickname task via mem_chat failed: {e}")

        if mem_chat_only and not force_mode:
            return self._job_result(False, "failed", "mem_chat_only_no_verified_path")

        decision = await self._decide_nickname(target_umo, topic=topic) if not force_card else {}
        proposed = force_card or str(decision.get("new_card") or "").strip()
        should_change = bool(decision.get("should_change", False)) or bool(proposed)
        if not should_change:
            return self._job_result(True, "no_change")

        ok, card = self._sanitize_card(proposed)
        if not ok or not card:
            return self._job_result(True, "rejected_invalid_card")

        dry_run = bool(job.get("options", {}).get("dry_run", self.state.get("dry_run", True)))
        decision_reason = str(decision.get("reason") or "no_reason")
        if dry_run:
            await self._send_text(
                target_umo,
                f"[昵称刷新-dryrun] 建议新群名片：{card}\n原因：{decision_reason}",
            )
            return self._job_result(True, "dry_run", card=card, reason=decision_reason)

        ok_set, reason = await self._set_self_group_card(target_umo, card, self_id_override=force_self_id)
        if (not ok_set) and force_mode and accept_unverified_success and str(reason or "").startswith("verify_failed("):
            ok_set = True
            reason = "accepted_without_internal_verify"
        if ok_set:
            runtime_rec = self._mark_job_success(job_id, card=card, reason=decision_reason)
            success_count = _safe_int(runtime_rec.get("success_count"), 0)
            if max_success_runs > 0:
                if notify_every_success:
                    await self._send_text(
                        target_umo,
                        (
                            f"[昵称刷新测试] 第{success_count}/{max_success_runs}次已更新群名片：{card}"
                            if reason == "ok"
                            else f"[昵称刷新测试] 第{success_count}/{max_success_runs}次已更新群名片：{card}（内部回读未确认，按测试模式记成功）"
                        ),
                    )
                if success_count >= max_success_runs:
                    self._disable_job(job_id, reason="max_success_runs_reached")
                    await self._send_text(
                        target_umo,
                        f"[昵称刷新测试] 已完成 {success_count} 次改名验证，任务已自动停用。",
                    )
                    return self._job_result(
                        True,
                        "updated_and_completed" if reason == "ok" else "updated_unverified_and_completed",
                        card=card,
                        completed_runs=success_count,
                    )
                return self._job_result(
                    True,
                    "updated" if reason == "ok" else "updated_unverified",
                    card=card,
                    completed_runs=success_count,
                )
            await self._send_text(target_umo, f"[昵称刷新] 已更新群名片：{card}")
            return self._job_result(True, "updated", card=card)
        await self._send_text(target_umo, f"[昵称刷新] 更新失败：{reason}\n建议名片：{card}")
        return self._job_result(False, "failed", reason, card=card)

    async def _set_self_group_card(
        self,
        target_umo: str,
        card: str,
        self_id_override: str = "",
    ) -> tuple[bool, str]:
        gid = self._extract_group_id(target_umo)
        if not gid:
            return False, "invalid_group"
        bot, self_id = await self._resolve_bot_and_self_id(gid, target_umo, self_id_override=self_id_override)
        if not bot or not self_id:
            return False, "missing_bot_or_self_id"
        try:
            await bot.set_group_card(group_id=int(gid), user_id=int(self_id), card=str(card or ""))
            # Verify with retries because OneBot adapters may lag behind the card update notice briefly.
            expected = str(card or "").strip()
            last_current = ""
            last_error = ""
            for _ in range(5):
                try:
                    probe_results: list[str] = []
                    for fetcher in (
                        lambda: bot.get_group_member_info(
                            group_id=int(gid),
                            user_id=int(self_id),
                            no_cache=True,
                        ),
                        lambda: bot.call_action(
                            "get_group_member_info",
                            group_id=int(gid),
                            user_id=int(self_id),
                            no_cache=True,
                        ),
                        lambda: bot.get_group_member_info(
                            group_id=int(gid),
                            user_id=int(self_id),
                            no_cache=False,
                        ),
                    ):
                        try:
                            info = await fetcher()
                            data = info.get("data", {}) if isinstance(info, dict) else {}
                            probe = str(data.get("card") or "").strip()
                            if probe:
                                probe_results.append(probe)
                        except Exception:
                            continue
                    if not probe_results:
                        try:
                            member_list = await bot.call_action(
                                "get_group_member_list",
                                group_id=int(gid),
                            )
                            members = member_list.get("data", []) if isinstance(member_list, dict) else []
                            for member in members:
                                if str(member.get("user_id") or "").strip() == str(self_id):
                                    probe = str(member.get("card") or "").strip()
                                    if probe:
                                        probe_results.append(probe)
                                        break
                        except Exception:
                            pass
                    if probe_results:
                        last_current = probe_results[0]
                    if expected and last_current == expected:
                        break
                except Exception as e:
                    last_error = str(e)
                await asyncio.sleep(1)
            if expected and last_current != expected:
                if last_error:
                    return False, f"verify_error({last_error})"
                return False, f"verify_failed(current={last_current})"
            self._nickname_commit(target_umo, str(card or ""))
            return True, "ok"
        except Exception as e:
            logger.warning(f"[mem_scheduler] direct set_group_card failed: {e}")
            return False, str(e)

    def _job_one_line(self, job: dict) -> str:
        trig = job.get("trigger", {})
        if str(trig.get("kind")) == "cron":
            t = f"cron={trig.get('expr')}"
        else:
            t = f"interval={trig.get('seconds')}s"
        return (
            f"{job.get('id')} | type={job.get('type')} | enabled={job.get('enabled')} | "
            f"dry_run={job.get('options', {}).get('dry_run', True)} | {t} | target={job.get('target_umo')}"
        )

    def _job_summary(self, job: dict) -> dict[str, Any]:
        payload = job.get("payload", {}) if isinstance(job.get("payload"), dict) else {}
        trigger = job.get("trigger", {}) if isinstance(job.get("trigger"), dict) else {}
        summary = {
            "id": str(job.get("id") or ""),
            "type": str(job.get("type") or ""),
            "enabled": bool(job.get("enabled", True)),
            "target_umo": str(job.get("target_umo") or ""),
            "trigger": trigger,
            "dry_run": bool(job.get("options", {}).get("dry_run", True)),
            "meta": job.get("meta", {}),
            "payload": payload,
        }
        runtime = self._get_job_runtime(summary["id"])
        if runtime:
            summary["runtime"] = runtime
        return summary

    def _save_job_and_schedule(self, job: dict) -> dict:
        self.jobs[job["id"]] = job
        if bool(job.get("enabled", True)):
            self._schedule_job(job["id"], job)
        else:
            try:
                self.scheduler.remove_job(job["id"])
            except Exception:
                pass
        self._save_jobs()
        return job

    def _commands_enabled(self) -> bool:
        return bool(self.config.get("enable_command_api", False))

    @filter.command_group("mcs")
    def mcs(self):
        """mem scheduler command group."""

    @mcs.command("jobs")
    async def mcs_jobs(self, event: AstrMessageEvent):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        if not self.jobs:
            yield event.plain_result("暂无任务。")
            return
        lines = ["当前任务列表："]
        for _, job in sorted(self.jobs.items(), key=lambda x: x[0]):
            lines.append(self._job_one_line(job))
        yield event.plain_result("\n".join(lines))

    @mcs.command("run")
    async def mcs_run(self, event: AstrMessageEvent, job_id: str):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        ok = await self._run_job(str(job_id), True)
        yield event.plain_result(f"run {job_id}: {'ok' if ok else 'failed'}")

    @mcs.command("enable")
    async def mcs_enable(self, event: AstrMessageEvent, job_id: str):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        jid = str(job_id or "")
        job = self.jobs.get(jid)
        if not job:
            yield event.plain_result("任务不存在。")
            return
        job["enabled"] = True
        job["meta"]["updated_at"] = int(_now_ts())
        self._schedule_job(jid, job)
        self._save_jobs()
        yield event.plain_result(f"已启用: {jid}")

    @mcs.command("disable")
    async def mcs_disable(self, event: AstrMessageEvent, job_id: str):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        jid = str(job_id or "")
        job = self.jobs.get(jid)
        if not job:
            yield event.plain_result("任务不存在。")
            return
        job["enabled"] = False
        job["meta"]["updated_at"] = int(_now_ts())
        try:
            self.scheduler.remove_job(jid)
        except Exception:
            pass
        self._save_jobs()
        yield event.plain_result(f"已停用: {jid}")

    @mcs.command("rm")
    async def mcs_rm(self, event: AstrMessageEvent, job_id: str):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        jid = str(job_id or "")
        if jid not in self.jobs:
            yield event.plain_result("任务不存在。")
            return
        self.jobs.pop(jid, None)
        try:
            self.scheduler.remove_job(jid)
        except Exception:
            pass
        self._save_jobs()
        yield event.plain_result(f"已删除: {jid}")

    @mcs.command("dryrun")
    async def mcs_dryrun(self, event: AstrMessageEvent, mode: str = "on"):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        mode_s = str(mode or "on").strip().lower()
        val = mode_s in {"on", "1", "true", "yes"}
        self.state["dry_run"] = bool(val)
        self._save_state()
        yield event.plain_result(f"全局 dry_run = {self.state['dry_run']}")

    @mcs.command("history")
    async def mcs_history(self, event: AstrMessageEvent, n: int = 10):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        n = max(1, min(50, _safe_int(n, 10)))
        rows = self.state.get("history", [])
        if not isinstance(rows, list) or not rows:
            yield event.plain_result("暂无执行历史。")
            return
        lines = []
        for r in rows[-n:]:
            ts = int(r.get("ts", 0))
            dt = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts > 0 else "?"
            lines.append(
                f"{dt} | {r.get('job_id')} | {r.get('status')} | {r.get('cost_ms', 0)}ms | {r.get('error', '')}"
            )
        yield event.plain_result("\n".join(lines))

    @mcs.command("set_card")
    async def mcs_set_card(self, event: AstrMessageEvent):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        raw = str(event.message_str or "")
        key = "set_card"
        pos = raw.find(key)
        if pos < 0:
            yield event.plain_result("参数解析失败。")
            return
        card = raw[pos + len(key) :].strip()
        if not card:
            yield event.plain_result("用法: /mcs set_card <新群名片>")
            return
        target_umo = str(event.unified_msg_origin or "")
        ok, reason = await self._set_self_group_card(target_umo, card)
        if ok:
            yield event.plain_result(f"已更新群名片: {card}")
        else:
            yield event.plain_result(f"更新失败: {reason}")

    @mcs.command("add_reminder")
    async def mcs_add_reminder(self, event: AstrMessageEvent):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        raw = str(event.message_str or "")
        key = "add_reminder"
        pos = raw.find(key)
        if pos < 0:
            yield event.plain_result("参数解析失败。")
            return
        tail = raw[pos + len(key) :].strip()
        fields = tail.split()
        if len(fields) < 6:
            yield event.plain_result("用法: /mcs add_reminder <cron_expr> <提醒文本>")
            return
        cron_expr = " ".join(fields[:5]).strip()
        text = " ".join(fields[5:]).strip()
        if not text:
            yield event.plain_result("用法: /mcs add_reminder <cron_expr> <提醒文本>")
            return
        target_umo = str(event.unified_msg_origin or "")
        job = self._normalize_job(
            {
                "type": "reminder",
                "enabled": True,
                "target_umo": target_umo,
                "trigger": {"kind": "cron", "expr": cron_expr},
                "payload": {"text": text},
                "options": {"dry_run": bool(self.state.get("dry_run", True))},
            },
            creator=f"{event.get_sender_name()}/{event.get_sender_id()}",
        )
        if not job:
            yield event.plain_result("创建失败：请检查 cron 表达式。")
            return
        self.jobs[job["id"]] = job
        self._schedule_job(job["id"], job)
        self._save_jobs()
        yield event.plain_result(f"已创建 reminder 任务: {job['id']}")

    @mcs.command("add_nickname")
    async def mcs_add_nickname(self, event: AstrMessageEvent):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        raw = str(event.message_str or "")
        key = "add_nickname"
        pos = raw.find(key)
        if pos < 0:
            yield event.plain_result("参数解析失败。")
            return
        tail = raw[pos + len(key) :].strip()
        fields = tail.split()
        if len(fields) < 5:
            yield event.plain_result("用法: /mcs add_nickname <cron_expr> [topic]")
            return
        cron_expr = " ".join(fields[:5]).strip()
        topic = " ".join(fields[5:]).strip() if len(fields) > 5 else ""
        target_umo = str(event.unified_msg_origin or "")
        job = self._normalize_job(
            {
                "type": "nickname_refresh",
                "enabled": True,
                "target_umo": target_umo,
                "trigger": {"kind": "cron", "expr": cron_expr},
                "payload": {"topic": topic},
                "options": {"dry_run": bool(self.state.get("dry_run", True))},
            },
            creator=f"{event.get_sender_name()}/{event.get_sender_id()}",
        )
        if not job:
            yield event.plain_result("创建失败：请检查 cron 表达式。")
            return
        self.jobs[job["id"]] = job
        self._schedule_job(job["id"], job)
        self._save_jobs()
        yield event.plain_result(f"已创建 nickname_refresh 任务: {job['id']}")

    @mcs.command("add_weekly_nickname")
    async def mcs_add_weekly_nickname(self, event: AstrMessageEvent):
        if not self._commands_enabled():
            yield event.plain_result("mcs 命令入口已禁用，请使用自然语言触发。")
            return
        self._capture_runtime_from_event(event)
        raw = str(event.message_str or "")
        key = "add_weekly_nickname"
        pos = raw.find(key)
        if pos < 0:
            yield event.plain_result("参数解析失败。")
            return
        tail = raw[pos + len(key) :].strip()
        fields = tail.split()
        window_start = "10:00"
        window_end = "23:00"
        topic = tail
        has_window = (
            len(fields) >= 2
            and re.match(r"^\d{1,2}:\d{1,2}$", fields[0])
            and re.match(r"^\d{1,2}:\d{1,2}$", fields[1])
        )
        if has_window:
            window_start = fields[0]
            window_end = fields[1]
            topic = " ".join(fields[2:]).strip()
        if not topic:
            topic = "根据当前群聊氛围和爱丽丝人设，通过 mem_chat 推送接口让 LLM 每周随机更新一次自己的群名片"
        target_umo = str(event.unified_msg_origin or "")
        job = self._normalize_job(
            {
                "type": "nickname_refresh",
                "enabled": True,
                "target_umo": target_umo,
                "trigger": {"kind": "interval", "seconds": 600},
                "payload": {
                    "topic": topic,
                    "must_change": True,
                    "random_weekly_once": True,
                    "window_start": window_start,
                    "window_end": window_end,
                },
                "options": {"dry_run": False},
            },
            creator=f"{event.get_sender_name()}/{event.get_sender_id()}",
        )
        if not job:
            yield event.plain_result("创建失败：请检查参数。")
            return
        self._save_job_and_schedule(job)
        yield event.plain_result(
            f"已创建每周随机群名片任务: {job['id']}（窗口 {window_start}-{window_end}）"
        )

    @filter.llm_tool()  # type: ignore
    async def llm_schedule_task(
        self,
        event: AstrMessageEvent,
        task_type: str,
        cron_expression: str = "",
        interval_sec: int = 0,
        text: str = "",
        topic: str = "",
        target_umo: str = "",
        enabled: bool = True,
        dry_run: bool = True,
        mention_qq: str = "",
        mention_name: str = "",
        repeat: bool = False,
        random_daily_once: bool = False,
        random_weekly_once: bool = False,
        window_start: str = "",
        window_end: str = "",
        must_change: bool = False,
        bypass_limits: bool = False,
    ):
        """
        self._capture_runtime_from_event(event)
        创建 mem_scheduler 定时任务。
        Args:
            task_type(string): 任务类型，可选 reminder / nickname_refresh。
            cron_expression(string): cron 表达式（5段），与 interval_sec 二选一。
            interval_sec(number): 间隔秒数，与 cron_expression 二选一。
            text(string): reminder 文本（task_type=reminder 时建议提供）。
            topic(string): nickname_refresh 的主题提示（可选）。
            target_umo(string): 目标会话，不填时默认当前会话。
            enabled(bool): 是否启用任务。
            dry_run(bool): 是否 dry_run（nickname_refresh 建议默认 true）。
        """
        t = str(task_type or "").strip().lower()
        if t not in {"reminder", "nickname_refresh"}:
            return "schedule_failed: invalid task_type"
        target = str(target_umo or event.unified_msg_origin or "").strip()
        if not target:
            return "schedule_failed: missing target_umo"

        trigger: dict[str, Any]
        if str(cron_expression or "").strip():
            trigger = {"kind": "cron", "expr": str(cron_expression).strip()}
        else:
            sec = max(1, _safe_int(interval_sec, 0))
            if sec <= 0:
                return "schedule_failed: missing trigger"
            trigger = {"kind": "interval", "seconds": sec}

        payload: dict[str, Any] = {}
        if t == "reminder":
            payload["text"] = str(text or "定时提醒：请处理计划事项。").strip()
            payload["once"] = not bool(repeat)
            mqq = str(mention_qq or event.get_sender_id() or "").strip()
            mname = str(mention_name or event.get_sender_name() or "").strip()
            if mqq:
                payload["mention_qq"] = mqq
            if mname:
                payload["mention_name"] = mname
        if t == "nickname_refresh":
            payload["topic"] = str(topic or "").strip()
            if bool(must_change):
                payload["must_change"] = True
            if bool(bypass_limits):
                payload["bypass_limits"] = True
            if bool(random_daily_once):
                payload["random_daily_once"] = True
            if bool(random_weekly_once):
                payload["random_weekly_once"] = True
            if str(window_start or "").strip():
                payload["window_start"] = str(window_start).strip()
            if str(window_end or "").strip():
                payload["window_end"] = str(window_end).strip()

        job = self._normalize_job(
            {
                "type": t,
                "enabled": bool(enabled),
                "target_umo": target,
                "trigger": trigger,
                "payload": payload,
                "options": {"dry_run": bool(dry_run)},
            },
            creator=f"{event.get_sender_name()}/{event.get_sender_id()}",
        )
        if not job:
            return "schedule_failed: invalid parameters"
        self._save_job_and_schedule(job)
        return f"schedule_ok: {json.dumps(self._job_summary(job), ensure_ascii=False)}"

    @filter.llm_tool()  # type: ignore
    async def llm_list_scheduled_tasks(
        self,
        event: AstrMessageEvent,
        target_umo: str = "",
        include_disabled: bool = False,
        limit: int = 20,
    ):
        """
        列出 mem_scheduler 当前任务。
        Args:
            target_umo(string): 仅查看指定会话的任务；留空查看全部。
            include_disabled(bool): 是否包含已停用任务。
            limit(number): 最多返回多少条，默认 20。
        """
        self._capture_runtime_from_event(event)
        target = str(target_umo or "").strip()
        rows = []
        for _, job in sorted(self.jobs.items(), key=lambda x: x[0]):
            if target and str(job.get("target_umo") or "") != target:
                continue
            if not include_disabled and not bool(job.get("enabled", True)):
                continue
            rows.append(self._job_summary(job))
        n = max(1, min(50, _safe_int(limit, 20)))
        return json.dumps(rows[:n], ensure_ascii=False)

    @filter.llm_tool()  # type: ignore
    async def llm_run_scheduled_task(self, event: AstrMessageEvent, job_id: str):
        """
        立即执行一次指定任务。
        Args:
            job_id(string): 任务 ID。
        """
        self._capture_runtime_from_event(event)
        jid = str(job_id or "").strip()
        if not jid or jid not in self.jobs:
            return "run_failed: job_not_found"
        ok = await self._run_job(jid, True)
        job = self.jobs.get(jid) or {}
        return json.dumps(
            {
                "job_id": jid,
                "ok": bool(ok),
                "job": self._job_summary(job) if job else None,
            },
            ensure_ascii=False,
        )

    @filter.llm_tool()  # type: ignore
    async def llm_set_scheduled_task_enabled(
        self,
        event: AstrMessageEvent,
        job_id: str,
        enabled: bool = True,
    ):
        """
        启用或停用指定任务。
        Args:
            job_id(string): 任务 ID。
            enabled(bool): true 为启用，false 为停用。
        """
        self._capture_runtime_from_event(event)
        jid = str(job_id or "").strip()
        job = self.jobs.get(jid)
        if not job:
            return "set_enabled_failed: job_not_found"
        job["enabled"] = bool(enabled)
        job["meta"]["updated_at"] = int(_now_ts())
        self._save_job_and_schedule(job)
        return json.dumps(self._job_summary(job), ensure_ascii=False)

    @filter.llm_tool()  # type: ignore
    async def llm_remove_scheduled_task(self, event: AstrMessageEvent, job_id: str):
        """
        删除指定任务。
        Args:
            job_id(string): 任务 ID。
        """
        self._capture_runtime_from_event(event)
        jid = str(job_id or "").strip()
        job = self.jobs.get(jid)
        if not job:
            return "remove_failed: job_not_found"
        removed = self._job_summary(job)
        self.jobs.pop(jid, None)
        try:
            self.scheduler.remove_job(jid)
        except Exception:
            pass
        self._save_jobs()
        return json.dumps({"removed": removed}, ensure_ascii=False)

    @filter.llm_tool()  # type: ignore
    async def llm_get_schedule_history(
        self,
        event: AstrMessageEvent,
        job_id: str = "",
        limit: int = 10,
    ):
        """
        查看任务执行历史。
        Args:
            job_id(string): 可选，仅查看指定任务。
            limit(number): 最多返回多少条，默认 10。
        """
        self._capture_runtime_from_event(event)
        jid = str(job_id or "").strip()
        rows = self.state.get("history", [])
        if not isinstance(rows, list):
            rows = []
        if jid:
            rows = [r for r in rows if str(r.get("job_id") or "") == jid]
        n = max(1, min(50, _safe_int(limit, 10)))
        return json.dumps(rows[-n:], ensure_ascii=False)
