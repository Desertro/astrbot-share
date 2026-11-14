"""self_agent: give the bot a way to actually go and do something.

Two ways in, one executor:

- natural language (default): when the owner @s the bot with something that
  looks like a work request, it goes and does it, then answers with findings.
- `#ask <request>` (escape hatch, always available for the owner).

Backends:
    codex    - `codex exec` in a subprocess, using an isolated CODEX_HOME
    builtin  - the small read/run loop in agent_core.py (works out of the box)
    dsh      - `dsh --profile headless` in a subprocess (legacy fallback)
"""
from __future__ import annotations
import asyncio
import datetime
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
try:
    from . import agent_core
except ImportError:
    import agent_core
DATA_DIR = Path(__file__).resolve().parents[2]
ASTRBOT_ROOT = DATA_DIR.parent
MEM_CHAT_CONF = DATA_DIR / 'config' / 'astrbot_plugin_mem_chat_config.json'
MEM_CHAT_DB = DATA_DIR / 'plugins' / 'astrbot_plugin_mem_chat' / 'mem_chat_embeddings.db'
SNAPSHOT_ROOT = DATA_DIR / 'plugin_data' / 'self_agent' / 'snapshots'
AGENT_WORKSPACE = Path(__import__('share_runtime').resolve('{{USER_HOME}}/alice/workspace'))
BOT_UID = '3830090475'
PLUGIN_COMMANDS = {'ask', 'rollback', 'yes', 'tasks'}
PENDING_DIR = DATA_DIR.parent / '.alice' / 'pending'
ASTRBOT_DB = DATA_DIR / 'data_v4.db'
MEME_KB_DIR = DATA_DIR / 'plugins' / 'astrbot_plugin_meme_kb'
MEME_DB = DATA_DIR / 'plugin_data' / 'meme_kb' / 'memes.db'
MEME_WORDS = ('梗', '梗图', '表情包', 'meme', '出处', '这个说法')

@register('self_agent', 'dsh', '让爱丽丝在群里真正去跑腿（Codex/DSH/内置后端）', '0.3.2')
class SelfAgent(Star):

    def __init__(self, context: Context, config: dict | None=None):
        super().__init__(context, config)
        self.conf = config or {}
        self._busy: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._snapshots: list[Path] = []

    def _c(self, key: str, default):
        value = self.conf.get(key, default)
        return default if value is None else value

    def _admin_uids(self) -> set[str]:
        raw = self._c('admin_uids', [])
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.split(',') if x.strip()]
        return {str(x).strip() for x in raw}

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return str(event.get_sender_id()) in self._admin_uids()

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """Report whether the sender is an AstrBot-wide admin.

        Private chat skips the @-mention requirement, so it needs a stricter
        gate than `_is_admin`: that one also honours this plugin's own
        `admin_uids`, which intentionally includes an engineering account that
        should stay group-only.

        Args:
            event: The incoming message event.

        Returns:
            True when the sender is listed in AstrBot's global `admins_id`.
        """
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _workdir(self) -> Path:
        raw = str(self._c('workdir', '')).strip()
        return Path(raw) if raw else ASTRBOT_ROOT

    def _creds(self) -> tuple[str, str, str]:
        base_url = str(self._c('coder_base_url', '')).strip()
        api_key = str(self._c('coder_api_key', '')).strip()
        model = str(self._c('coder_model', '')).strip()
        if (not base_url or not api_key) and MEM_CHAT_CONF.exists():
            try:
                conf = json.loads(MEM_CHAT_CONF.read_text(encoding='utf-8-sig'))
                base_url = base_url or str(conf.get('llm_base_url', '')).strip()
                api_key = api_key or str(conf.get('llm_api_key', '')).strip()
                model = model or str(conf.get('llm_model', '')).strip()
            except Exception:
                pass
        return (base_url, api_key, model or 'deepseek-flash')

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """Catch a work request addressed to the bot and actually run it."""
        if not bool(self._c('nl_enabled', True)):
            return
        if not self._is_admin(event):
            return
        text = (event.message_str or '').strip()
        if any(('astrbot_plugin_shoubanhua' in str(getattr(handler, 'handler_module_path', '')).split('.') for handler in event.get_extra('activated_handlers', []) or [])):
            self.logger.info('[self_agent] Passing matched image command to FigurinePro.')
            return
        if self._looks_like_capability_request(text):
            self.logger.info(f'[self_agent] Passing capability request to mem_chat: {text[:60]!r}')
            return
        if self._looks_like_meme_capture(text):
            return
        if self._looks_like_schedule(text):
            await self._set_schedule(event, text)
            return
        await self._note_followup(event, text)
        if not text or text.startswith('#'):
            return
        if text.split(maxsplit=1)[0].lower() in PLUGIN_COMMANDS:
            return
        umo = event.unified_msg_origin
        group_id = event.get_group_id()
        if not group_id and (not bool(self._c('private_nl_enabled', False))):
            self.logger.info(f'[self_agent] private NL auto-run disabled; leaving chat to mem_chat text={text[:40]!r}')
            return
        self.logger.info(f'[self_agent] gate umo={umo} at={event.is_at_or_wake_command} group={group_id} text={text[:40]!r}')
        if group_id:
            if not event.is_at_or_wake_command:
                return
        elif not self._is_owner(event):
            self.logger.info(f'[self_agent] private chat refused for {event.get_sender_id()}')
            return
        is_task = await self._looks_like_task(text, context=self._quoted_context(event))
        self.logger.info(f'[self_agent] classify={is_task} text={text[:40]!r}')
        if not is_task:
            return
        lock = self._locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            self.logger.info(f'[self_agent] queued behind a running job: {umo}')
            await event.send(event.plain_result('上一个还在这只手上，这个排队。'))
        async with lock:
            await self._run_job(event, text)

    async def initialize(self) -> None:
        """Re-attach handlers and install the bot's own recurring jobs."""
        await self._rearm_reminders()
        await self._install_own_jobs()

    async def _fire_reminder(self, umo: str='', what: str='') -> None:
        """Deliver one occurrence of a reminder.

        The cron manager splats the stored payload into this call, so the
        arguments must match the payload keys.

        Args:
            umo: Unified message origin the reminder goes to.
            what: Text to send.
        """
        if not umo or not what:
            return
        try:
            await self.context.send_message(umo, MessageChain().message(what))
        except Exception as exc:
            self.logger.warning(f'[self_agent] cron send failed: {exc}')

    async def _idle_chat(self, group_id: str='', slot: str='') -> None:
        """Decide on the spot whether to say something, then say it.

        Runs at a few fixed times a day. The model sees what the group has
        been talking about and may well decide to stay quiet, which is the
        point: a bot that always speaks on schedule reads as a bot.

        Args:
            group_id: Group to consider speaking in.
            slot: Rough time of day, passed to the model as context.
        """
        if not group_id or not bool(self._c('idle_chat_enabled', True)):
            return
        base_url, api_key, model = self._creds()
        if not base_url or not api_key:
            return
        recent: list[str] = []
        try:
            cfg = json.loads(Path(__import__('share_runtime').resolve('{{USER_HOME}}/alice/config.json')).read_text(encoding='utf-8'))
            api = str(cfg.get('api', 'http://127.0.0.1:3210')).rstrip('/')
            req = urllib.request.Request(f'{api}/get_group_msg_history', data=json.dumps({'group_id': int(group_id), 'count': 20}).encode(), headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {cfg['token']}'})
            with urllib.request.urlopen(req, timeout=20) as resp:
                msgs = json.loads(resp.read()).get('data', {}).get('messages', [])
            now = time.time()
            for m in msgs:
                who = (m.get('sender') or {}).get('nickname', '')[:10]
                mine = str((m.get('sender') or {}).get('user_id')) == BOT_UID
                body = re.sub('\\[CQ:[^\\]]*\\]', '', m.get('raw_message', '')).strip()
                if body:
                    mins = int((now - m.get('time', now)) / 60)
                    recent.append(f'{('我' if mine else who)}（{mins}分钟前）：{body[:60]}')
        except Exception as exc:
            self.logger.warning(f'[self_agent] idle read failed: {exc}')
        quiet_for = 999
        if recent:
            hit = re.search('（(\\d+)分钟前）', recent[-1])
            if hit:
                quiet_for = int(hit.group(1))
        prompt = '你是天童爱丽丝，在一个 QQ 群里。现在是' + (slot or '白天') + f'，群里最后一条消息是 {quiet_for} 分钟前。\n下面是群里最近说的话（没有就是一直没人说）：\n' + ('\n'.join(recent[-12:]) if recent else '（空）') + '\n\n你现在要决定：要不要主动说一句话。\n判断标准：群里正热闹就别插话；刚有人说过话也别插；很久没人说话、或者话题正好能接上、或者这个时间点有话可说，才开口。\n只输出 JSON，无解释：{"speak":true/false,"text":""}\nspeak 为 false 时 text 留空。speak 为 true 时 text 是你要说的话，一句，不超过25字，用你平时的口吻，不要打招呼式的开场。'
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as sess:
                async with sess.post(f'{base_url.rstrip('/')}/chat/completions', headers={'Authorization': f'Bearer {api_key}'}, json={'model': model, 'max_tokens': 1200, 'messages': [{'role': 'user', 'content': prompt}]}) as resp:
                    if resp.status != 200:
                        return
                    body = await resp.json()
            raw = body['choices'][0]['message']['content']
            hit = re.search('\\{.*\\}', raw, re.S)
            if not hit:
                return
            plan = json.loads(hit.group(0))
        except Exception as exc:
            self.logger.warning(f'[self_agent] idle think failed: {exc}')
            return
        if not plan.get('speak') or not str(plan.get('text', '')).strip():
            self.logger.info(f'[self_agent] idle {slot} {group_id}: stayed quiet')
            return
        said = str(plan['text']).strip()[:60]
        try:
            await self.context.send_message(f'default:GroupMessage:{group_id}', MessageChain().message(said))
            self.logger.info(f'[self_agent] idle {slot} {group_id}: {said}')
        except Exception as exc:
            self.logger.warning(f'[self_agent] idle send failed: {exc}')

    async def _note_followup(self, event: AstrMessageEvent, text: str) -> None:
        """Let the bot set its own reminder when someone says they are busy.

        "我去吃饭了" is worth coming back to in half an hour. Nobody asked for
        a reminder, which is exactly why it reads as attention rather than
        automation.

        Args:
            event: The message that suggests a follow-up.
            text: Its plain text.
        """
        group_id = event.get_group_id()
        if not group_id or not bool(self._c('self_followup_enabled', True)):
            return
        hit = re.search('(我?(先|去|要去)?(吃饭|睡|洗澡|上课|开会|出门|打球|加班|考试|上班)|等我|马上回|一会儿?回|待会儿?回)', text)
        if not hit:
            return
        sender = str(event.get_sender_id())
        marker = f'#self:{sender}'
        try:
            jobs = await self.context.cron_manager.list_jobs()
            if any((marker in str(getattr(j, 'description', '')) for j in jobs)):
                return
        except Exception:
            return
        delay = 30 if '吃饭' in text or '洗澡' in text else 60
        when = datetime.datetime.now() + datetime.timedelta(minutes=delay)
        what = hit.group(0)
        nick = event.get_sender_name() or '你'
        umo = event.unified_msg_origin
        try:
            await self.context.cron_manager.add_basic_job(name=f'alice:惦记{nick[:6]}', cron_expression=f'{when.minute} {when.hour} * * *', handler=self._fire_followup, description=f'{umo} | 自己记的：{what} {marker}', timezone='Asia/Shanghai', payload={'umo': umo, 'who': nick, 'what': what, 'uid': sender}, persistent=False)
            self.logger.info(f'[self_agent] self-followup in {delay}min for {nick}: {what}')
        except Exception as exc:
            self.logger.warning(f'[self_agent] self-followup failed: {exc}')

    async def _fire_followup(self, umo: str='', who: str='', what: str='', uid: str='') -> None:
        """Ask how it went, phrased on the spot rather than from a template."""
        if not umo:
            return
        base_url, api_key, model = self._creds()
        said = ''
        if base_url and api_key:
            prompt = f'你是天童爱丽丝。{who} 之前说「{what}」，过了一会儿了。你想顺口问一句他现在怎么样，一句话，不超过20字，用你平时的口吻，不要像客服。只输出这句话。'
            try:
                import aiohttp
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as sess:
                    async with sess.post(f'{base_url.rstrip('/')}/chat/completions', headers={'Authorization': f'Bearer {api_key}'}, json={'model': model, 'max_tokens': 800, 'messages': [{'role': 'user', 'content': prompt}]}) as resp:
                        if resp.status == 200:
                            body = await resp.json()
                            said = str(body['choices'][0]['message']['content']).strip().strip('"')[:50]
            except Exception as exc:
                self.logger.warning(f'[self_agent] followup think failed: {exc}')
        if not said:
            return
        try:
            chain = MessageChain()
            if uid:
                chain = chain.at(qq=int(uid), name=who or '')
            await self.context.send_message(umo, chain.message(said))
            self.logger.info(f'[self_agent] followup sent: {said}')
        except Exception as exc:
            self.logger.warning(f'[self_agent] followup send failed: {exc}')

    async def _self_check(self) -> None:
        """Look over own state once a day and report only real problems."""
        notes: list[str] = []
        root = self._workdir()
        try:
            db = root / 'data' / 'memes_data' / 'meme_index.db'
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
            try:
                tags = con.execute('SELECT COUNT(*) FROM meme_tags').fetchone()[0]
                novec = con.execute('SELECT COUNT(*) FROM meme_tags t LEFT JOIN meme_index i ON t.path = i.path WHERE i.path IS NULL').fetchone()[0]
            finally:
                con.close()
            files = sum((1 for _ in (root / 'data' / 'memes_data' / 'memes').rglob('*') if _.is_file()))
            if novec:
                notes.append(f'{novec}张表情没建索引，检索用不上')
            if files - tags > 3:
                notes.append(f'表情文件{files}个但只标了{tags}个')
        except Exception as exc:
            notes.append(f'表情库查不了：{str(exc)[:40]}')
        try:
            audit = Path(__import__('share_runtime').resolve('{{ASTRBOT_ROOT}}/.alice/audit.jsonl'))
            if audit.is_file():
                cutoff = time.time() - 86400
                blocked = 0
                for line in audit.read_text(encoding='utf-8', errors='replace').splitlines()[-500:]:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get('ts', 0) < cutoff:
                        continue
                    if row.get('action') == 'blocked':
                        blocked += 1
                if blocked:
                    notes.append(f'昨天有{blocked}次操作被拦下来')
        except Exception:
            pass
        if Path(__import__('share_runtime').resolve('{{USER_HOME}}/.alice.stop')).exists():
            notes.append('急停开关是开着的，爱丽丝动不了手')
        if not notes:
            self.logger.info('[self_agent] self-check: all clear')
            return
        self.logger.info(f'[self_agent] self-check: {notes}')
        owner = (self._admin_uids() or [__import__('share_runtime').resolve('{{AB_QQ_1}}')])[0]
        try:
            await self.context.send_message(f'default:FriendMessage:{owner}', MessageChain().message('爱丽丝自检：' + '；'.join(notes[:3])))
        except Exception as exc:
            self.logger.warning(f'[self_agent] self-check report failed: {exc}')

    async def _install_own_jobs(self) -> None:
        """Register the bot's own recurring jobs, replacing any stale copies."""
        mgr = self.context.cron_manager
        try:
            existing = {str(getattr(j, 'name', '')): j for j in await mgr.list_jobs()}
        except Exception as exc:
            self.logger.warning(f'[self_agent] cannot list jobs: {exc}')
            return
        groups = self._c('idle_chat_groups', []) or []
        if isinstance(groups, str):
            groups = [x.strip() for x in groups.split(',') if x.strip()]
        slots = [('早上', '20 9'), ('午后', '40 14'), ('深夜', '10 23')]
        wanted: list[tuple[str, str, object, dict]] = []
        for gid in groups:
            for slot, expr in slots:
                wanted.append((f'alice:自己冒泡{slot}{gid}', f'{expr} * * *', self._idle_chat, {'group_id': str(gid), 'slot': slot}))
        wanted.append(('alice:自检', '30 8 * * *', self._self_check, {}))
        for name, expr, handler, payload in wanted:
            old = existing.get(name)
            if old:
                mgr._basic_handlers[old.job_id] = handler
                continue
            try:
                await mgr.add_basic_job(name=name, cron_expression=expr, handler=handler, description='爱丽丝自己的日程 #own', timezone='Asia/Shanghai', payload=payload, persistent=True)
                self.logger.info(f"[self_agent] own job added: {name} '{expr}'")
            except Exception as exc:
                self.logger.warning(f'[self_agent] add own job {name}: {exc}')

    async def _rearm_reminders(self) -> None:
        """Re-attach handlers to reminders restored from the database.

        `add_basic_job` keeps handlers in memory only, so without this every
        persisted reminder would fail with "handler not found" after a
        restart.
        """
        try:
            mgr = self.context.cron_manager
            for job in await mgr.list_jobs():
                if str(getattr(job, 'name', '')).startswith('alice:'):
                    mgr._basic_handlers[job.job_id] = self._fire_reminder
            self.logger.info('[self_agent] reminders re-armed')
        except Exception as exc:
            self.logger.warning(f'[self_agent] re-arm failed: {exc}')

    def _looks_like_schedule(self, text: str) -> bool:
        """Tell whether a message is asking for a recurring reminder.

        Args:
            text: Plain text of the message.

        Returns:
            True when the text names a time and asks for something repeated.
        """
        has_time = bool(re.search('\\d{1,2}\\s*[:：]\\s*\\d{2}', text) or re.search('[\\d一二三四五六七八九十两]{1,3}\\s*点(半|[\\d一二三四五六七八九十]{1,3}\\s*分?)?', text) or re.search('(每天|每日|每晚|每早|工作日|周[一二三四五六日天])', text) or re.search('(早上|上午|中午|下午|傍晚|晚上|凌晨|半夜)', text))
        wants = bool(re.search('(定时|到点|提醒|(?<!我)叫|喊|催|通知|发条?消息|报时)', text))
        return has_time and wants

    async def _set_schedule(self, event: AstrMessageEvent, text: str) -> None:
        """Turn a natural-language request into a cron job.

        Args:
            event: Incoming message; its group becomes the delivery target.
            text: The request, parsed by the chat model into cron fields.
        """
        group_id = event.get_group_id()
        if not group_id:
            await event.send(event.plain_result('在群里跟爱丽丝说，私聊定不了群任务。'))
            event.stop_event()
            return
        sender = str(event.get_sender_id())
        quota = int(self._c('schedule_quota_per_user', 3))
        if not self._is_owner(event):
            try:
                mine = [j for j in await self.context.cron_manager.list_jobs() if str(getattr(j, 'description', '')).endswith(f'#{sender}')]
            except Exception:
                mine = []
            if len(mine) >= quota:
                await event.send(event.plain_result(f'你已经定了{len(mine)}个了，爱丽丝记不下更多。先说「取消」删掉一个吧。'))
                event.stop_event()
                return
        base_url, api_key, model = self._creds()
        spec = None
        if base_url and api_key:
            prompt = f'把这句话转成定时任务，只输出 JSON，无解释：\n{{"minute":"","hour":"","dow":"*","what":"","name":""}}\nminute/hour 是数字，dow 是 * 或 0-6（周一=1），what 是到点要发的话（用爱丽丝的口吻，一句），name 是任务简称（4-8字）。\n这句话：{text}'
            try:
                import aiohttp
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as sess:
                    async with sess.post(f'{base_url.rstrip('/')}/chat/completions', headers={'Authorization': f'Bearer {api_key}'}, json={'model': model, 'max_tokens': 900, 'messages': [{'role': 'user', 'content': prompt}]}) as resp:
                        if resp.status == 200:
                            body = await resp.json()
                            raw = body['choices'][0]['message']['content']
                            hit = re.search('\\{.*\\}', raw, re.S)
                            if hit:
                                spec = json.loads(hit.group(0))
            except Exception as exc:
                self.logger.warning(f'[self_agent] schedule parse failed: {exc}')
        if not spec or not str(spec.get('hour', '')).strip():
            await event.send(event.plain_result('爱丽丝没听懂几点，说清楚点？'))
            event.stop_event()
            return
        minute = str(spec.get('minute', '0')).strip() or '0'
        hour = str(spec.get('hour')).strip()
        dow = str(spec.get('dow', '*')).strip() or '*'
        what = str(spec.get('what', '')).strip() or '到点啦。'
        name = str(spec.get('name', '')).strip() or '定时提醒'
        expr = f'{minute} {hour} * * {dow}'
        umo = event.unified_msg_origin
        try:
            job = await self.context.cron_manager.add_basic_job(name=f'alice:{name}', cron_expression=expr, handler=self._fire_reminder, description=f'{umo} | {text[:60]} #{sender}', timezone='Asia/Shanghai', payload={'umo': umo, 'what': what}, persistent=True)
        except Exception as exc:
            self.logger.warning(f'[self_agent] add cron failed: {exc}')
            await event.send(event.plain_result('定时没挂上，等会再试。'))
            event.stop_event()
            return
        self.logger.info(f"[self_agent] cron added {job.job_id} '{expr}' -> {umo}: {what[:40]}")
        when = f'{hour}点{minute}分' if minute != '0' else f'{hour}点整'
        every = '每天' if dow == '*' else f'每周{dow}'
        await event.send(event.plain_result(f'记下了，{every}{when}爱丽丝来说。'))
        event.stop_event()

    @filter.command('tasks')
    async def cmd_tasks(self, event: AstrMessageEvent):
        """List or delete the reminders this plugin created."""
        if not self._is_owner(event):
            return
        arg = (event.message_str or '').replace('#tasks', '').strip()
        try:
            jobs = [j for j in await self.context.cron_manager.list_jobs() if str(getattr(j, 'name', '')).startswith('alice:')]
        except Exception as exc:
            yield event.plain_result(f'查不了：{exc}')
            return
        if arg.startswith('del'):
            target = arg.replace('del', '').strip()
            for j in jobs:
                if target and (target in j.job_id or target in j.name):
                    await self.context.cron_manager.delete_job(j.job_id)
                    yield event.plain_result(f'删了「{j.name[6:]}」。')
                    return
            yield event.plain_result('没找到这个任务。')
            return
        if not jobs:
            yield event.plain_result('爱丽丝现在没有定时任务。')
            return
        lines = []
        for j in jobs:
            nxt = self.context.cron_manager.get_next_run_time(j.job_id)
            when = nxt.strftime('%m-%d %H:%M') if nxt else '未排'
            lines.append(f'{j.name[6:]}（{j.cron_expression}）下次 {when}')
        yield event.plain_result('爱丽丝的定时任务：\n' + '\n'.join(lines))

    def _looks_like_capability_request(self, text: str) -> bool:
        """Recognize requests owned by the plugin capability agent.

        These messages must keep flowing to mem_chat so its LLM can invoke the
        ``plugin_agent`` tool. Running them through self_agent would execute a
        general coding agent instead and stop the event before plugin dispatch.

        Args:
            text: Plain text of the incoming message.

        Returns:
            True when the message is clearly about meme, image, or figurine
            plugin functionality.
        """
        normalized = (text or '').casefold()
        if not normalized:
            return False
        if re.search('(?<![a-z0-9_])(?:meme|memes|memelite|bnn|bna)(?![a-z0-9_])', normalized):
            return True
        plugin_terms = ('表情包', '梗图', '手办化', '生图', '生成图片', '生成图像', '图片生成', '图像生成', '生图预设', '改图', 'p图')
        if any((term in normalized for term in plugin_terms)):
            return True
        if 'gpt' in normalized and re.search('(图|image|画|生成|手办|风格|改图|p图)', normalized):
            return True
        return False

    def _looks_like_meme_capture(self, text: str) -> bool:
        """Route sticker requests to the chat LLM without executing them.

        Args:
            text: Plain text of the incoming message.

        Returns:
            Whether sticker collection should be left to the chat LLM.
        """
        return bool(re.search('(存|收|收藏|记|留)[下住着]?.{0,6}(表情|图|这张|这个|它|这些)', text) or re.search('(表情|图|这张|这个|这些)[^。]{0,8}(存|收|收藏)', text) or re.search('(?:存|收藏|保存)(?:到|进|入)\\s*[A-Za-z_]+', text))

    async def _run_job(self, event: AstrMessageEvent, text: str) -> None:
        """Run one request to completion: snapshot, execute, answer."""
        umo = event.unified_msg_origin
        self._busy.add(umo)
        started = time.time()
        ping = asyncio.create_task(self._progress_ping(event))
        try:
            self._sync_skills()
            snapshot = self._snapshot()
            if snapshot:
                self.logger.info(f'[self_agent] snapshot before edit: {snapshot}')
            ack = str(self._c('ack_text', '')).strip()
            if ack:
                await event.send(event.plain_result(ack))
            answer, trace = await self._execute(await self._build_prompt(event, text))
            ping.cancel()
            elapsed = time.time() - started
            limit = int(self._c('answer_max_chars', 1200))
            body = agent_core.format_for_chat(answer) or '嗯……爱丽丝没看出来。'
            if len(body) > limit:
                body = body[:limit] + '…'
            if bool(self._c('show_stats', False)):
                body = f'{body}\n\n（{len(trace)} 步，{elapsed:.1f}s）'
            await event.send(event.plain_result(body))
            asyncio.create_task(self._learn_meme(text, answer))
            event.stop_event()
        except Exception as exc:
            ping.cancel()
            self.logger.error(f'[self_agent] nl failed: {exc}')
            await event.send(event.plain_result('……卡住了，这次没搬动。'))
        finally:
            self._busy.discard(umo)

    def _sync_skills(self) -> None:
        """Mirror the Windows skill folder into the WSL DSH home.

        The user edits skills on the Windows side while the bot runs inside WSL,
        so without this the headless agent would see a stale or empty catalog.
        """
        backend = str(self._c('backend', 'builtin')).strip().lower()
        if backend in ('codex', 'auto'):
            return
        src = Path(str(self._c('skills_sync_dir', '/mnt/c/Users/Desertro/.dsh/skills')))
        dst = Path(str(self._c('skills_dir', __import__('share_runtime').resolve('{{USER_HOME}}/.dsh/skills'))))
        run_as = str(self._c('run_as_user', '')).strip()
        if run_as:
            import pwd
            try:
                dst = Path(pwd.getpwnam(run_as).pw_dir) / '.dsh' / 'skills'
            except KeyError:
                pass
        if not src.is_dir():
            return
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                target = dst / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, target)
        except Exception as exc:
            self.logger.warning(f'[self_agent] skill sync failed: {exc}')

    def _snapshot(self) -> Path | None:
        """Copy plugin source files aside so a bad edit can be undone.

        Only text-ish plugin files are copied: the databases and sticker assets
        are large and are never rewritten by the agent.

        Returns:
            The snapshot directory, or None when snapshots are disabled or fail.
        """
        if not bool(self._c('snapshot', True)):
            return None
        plugins = self._workdir() / 'data' / 'plugins'
        if not plugins.is_dir():
            return None
        configured = str(self._c('snapshot_dir', '')).strip()
        root = Path(configured) if configured else SNAPSHOT_ROOT
        target = root / time.strftime('%Y%m%d-%H%M%S')
        keep = max(1, int(self._c('snapshot_keep', 10)))
        try:
            for path in plugins.rglob('*'):
                if not path.is_file():
                    continue
                if path.suffix not in ('.py', '.json', '.yaml', '.yml', '.md'):
                    continue
                dest = target / path.relative_to(plugins)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            existing = sorted((d for d in root.iterdir() if d.is_dir()))
            for stale in existing[:-keep]:
                shutil.rmtree(stale, ignore_errors=True)
            return target
        except Exception as exc:
            self.logger.warning(f'[self_agent] snapshot failed: {exc}')
            return None

    async def _progress_ping(self, event: AstrMessageEvent) -> None:
        """Optionally say something when a job drags.

        Off by default: a fixed line repeated on every slow job reads like the
        bot, not like her. Set `progress_text` to one line or several separated
        by `|` if you want the nudge back.
        """
        delay = int(self._c('progress_after_sec', 0))
        options = [p.strip() for p in str(self._c('progress_text', '')).split('|')]
        options = [p for p in options if p]
        if delay <= 0 or not options:
            return
        try:
            await asyncio.sleep(delay)
            text = options[int(time.time()) % len(options)]
            await event.send(event.plain_result(text))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _looks_like_task(self, text: str, context: str='') -> bool:
        if len(text) > 400:
            return False
        base_url, api_key, model = self._creds()
        if not api_key:
            return False
        return await agent_core.classify_task(base_url=base_url, api_key=api_key, model=model, text=text, context=context, log=self.logger.info)

    def _kb(self):
        """Import the meme_kb helper module (it has no AstrBot imports of its own)."""
        if not MEME_KB_DIR.exists():
            return None
        if str(MEME_KB_DIR) not in sys.path:
            sys.path.insert(0, str(MEME_KB_DIR))
        try:
            import kb as meme_kb
        except Exception as exc:
            self.logger.warning(f'[self_agent] meme_kb import failed: {exc}')
            return None
        return meme_kb

    async def _meme_hits(self, text: str) -> list[dict]:
        """Look the current message up in the meme knowledge base."""
        if not bool(self._c('meme_kb_enabled', True)):
            return []
        module = self._kb()
        if module is None:
            return []
        embed_base, embed_key, embed_model, _, _ = self._creds_meme()
        try:
            return await module.search(MEME_DB, text, base_url=embed_base, api_key=embed_key, model=embed_model, k=int(self._c('meme_kb_k', 3)), min_score=float(self._c('meme_kb_min_score', 0.5)))
        except Exception as exc:
            self.logger.warning(f'[self_agent] meme search failed: {exc}')
            return []

    def _creds_meme(self) -> tuple[str, str, str, str, str]:
        """embed base/key/model plus chat base/key, reusing mem_chat's settings."""
        conf = {}
        if MEM_CHAT_CONF.exists():
            try:
                conf = json.loads(MEM_CHAT_CONF.read_text(encoding='utf-8-sig'))
            except Exception:
                conf = {}
        return (str(conf.get('embedding_openai_base_url', __import__('share_runtime').resolve('{{AB_MEM_CHAT_EMBEDDING_OPENAI_BASE_URL}}'))), str(conf.get('embedding_openai_api_key', '')), str(conf.get('embedding_openai_model', 'BAAI/bge-m3')), str(conf.get('llm_base_url', __import__('share_runtime').resolve('{{AB_CMD_PROVIDER_SOURCES_2_API_BASE}}'))), str(conf.get('llm_api_key', '')))

    async def _learn_meme(self, text: str, answer: str) -> None:
        """Let the bot decide for itself whether this exchange is worth keeping."""
        if not bool(self._c('meme_auto_learn', True)):
            return
        if len(text.strip()) < 4:
            return
        module = self._kb()
        if module is None:
            return
        embed_base, embed_key, embed_model, chat_base, chat_key = self._creds_meme()
        if not (embed_key and chat_key):
            return
        try:
            item = await agent_core.judge_meme(base_url=chat_base, api_key=chat_key, model=str(self._c('meme_judge_model', 'deepseek-flash')), text=f'【对方】{text}\n\n【爱丽丝】{answer[:1200]}', log=self.logger.info)
            if not item:
                return
            row_id, created, _ = await module.upsert(MEME_DB, item, base_url=embed_base, api_key=embed_key, model=embed_model, source='auto', dedupe_score=float(self._c('meme_dedupe_score', 0.92)))
            self.logger.info(f'[self_agent] meme {('added' if created else 'merged')} id={row_id} {item.get('term')}')
        except Exception as exc:
            self.logger.warning(f'[self_agent] meme learn failed: {exc}')

    def _persona(self) -> str:
        """Read the bot's real persona from AstrBot's DB — no invented voice."""
        cached = getattr(self, '_persona_cache', None)
        if cached is not None:
            return cached
        text = ''
        persona_id = str(self._c('persona_id', '')).strip()
        try:
            con = sqlite3.connect(f'file:{ASTRBOT_DB}?mode=ro', uri=True, timeout=3)
            if persona_id:
                row = con.execute('select system_prompt from personas where persona_id = ?', (persona_id,)).fetchone()
            else:
                row = con.execute("select system_prompt from personas where system_prompt != '' order by id limit 1").fetchone()
            con.close()
            text = (row[0] or '').strip() if row else ''
        except Exception as exc:
            self.logger.warning(f'[self_agent] persona load failed: {exc}')
        self._persona_cache = text
        return text

    def _recent_chat(self, group_id: str | None, limit: int) -> list[str]:
        """Pull the last few group messages so a bare "你搜一下" still makes sense."""
        if not group_id or limit <= 0 or (not MEM_CHAT_DB.exists()):
            return []
        try:
            con = sqlite3.connect(f'file:{MEM_CHAT_DB}?mode=ro', uri=True, timeout=3)
            rows = con.execute('select sender_name, text from raw_group_messages where group_id = ? order by created_at desc limit ?', (str(group_id), limit)).fetchall()
            con.close()
        except Exception:
            return []
        lines = []
        for name, text in reversed(rows):
            text = ' '.join(str(text or '').split())
            if not text or text.startswith('[图片内容'):
                continue
            lines.append(f'{name}: {text[:150]}')
        return lines

    def _quoted_context(self, event: AstrMessageEvent) -> str:
        """Return a short quoted-message context for intent classification."""
        try:
            for comp in event.message_obj.message:
                if comp.__class__.__name__ != 'Reply':
                    continue
                quoted_text = ' '.join(str(getattr(comp, 'message_str', '') or '').split())
                quoted_by = getattr(comp, 'sender_nickname', '') or '对方'
                if quoted_text:
                    return f'{quoted_by}: {quoted_text[:200]}'
        except Exception:
            pass
        return ''

    async def _build_prompt(self, event: AstrMessageEvent, text: str) -> str:
        group_id = event.get_group_id()
        lines = self._recent_chat(group_id, int(self._c('context_lines', 10)))
        sender = ''
        try:
            sender = event.get_sender_name() or ''
        except Exception:
            pass
        quoted = self._quoted_context(event)
        use_md = bool(self._c('use_agents_md', True))
        persona = '' if use_md else self._persona()
        if persona:
            host = f'【你是谁】\n{persona}\n\n'
        else:
            host = ''
        parts = [host]
        if lines:
            parts.append('【群里刚聊了什么】\n' + '\n'.join(lines))
        if quoted:
            parts.append('【他引用了这句】\n' + quoted)
        hits = await self._meme_hits(text)
        if hits:
            memo = '\n'.join((f'- {h['term']}：{h['meaning']}' for h in hits))
            parts.append('【你以前记过的梗，能用就顺着用】\n' + memo)
        parts.append(f'【{sender or '对方'}刚说】{text}')
        parts.append('【怎么回】直接写出你要发到群里的那条消息，用上面的身份和口吻，一到两句，别解释你查了什么工具、跑了什么命令，也不要贴统计信息。背景资料给你是让你心里有数，不是让你复述。')
        return '\n\n'.join(parts)

    @filter.command('ask')
    async def cmd_ask(self, event: AstrMessageEvent, args: GreedyStr):
        """#ask <你想让它去做的事>（主人专用，自然语言是默认入口）"""
        if not self._is_admin(event):
            yield event.plain_result('这个只对主人开放。')
            return
        question = str(args).strip()
        if not question:
            yield event.plain_result('直接把要我做的事说给我就行，比如：@我 看一眼日志有没有报错')
            return
        started = time.time()
        yield event.plain_result('好，我去看看。')
        try:
            answer, trace = await self._execute(await self._build_prompt(event, question))
        except Exception as exc:
            self.logger.error(f'[self_agent] ask failed: {exc}')
            yield event.plain_result(f'出错了：{exc}')
            return
        elapsed = time.time() - started
        text = answer.strip() or '我看了一圈，没得出结论。'
        limit = int(self._c('answer_max_chars', 900))
        if len(text) > limit:
            text = text[:limit] + '…'
        yield event.plain_result(text + f'\n\n（{len(trace)} 步，{elapsed:.1f}s）')

    @filter.command('yes')
    async def cmd_yes(self, event: AstrMessageEvent, args: GreedyStr):
        """#yes <请求id>（仅主人）批准一个等待确认的破坏性操作。

        qqctl parks destructive calls as pending requests instead of trusting
        a self-supplied flag; this mints the one-shot ticket that lets the
        parked command through.
        """
        if not self._is_owner(event):
            yield event.plain_result('只有主人能批。')
            return
        pending = PENDING_DIR
        req = str(args).strip()
        if not req:
            items = sorted(pending.glob('*.json')) if pending.is_dir() else []
            if not items:
                yield event.plain_result('没有等确认的请求。')
                return
            lines = ['等你确认的请求：']
            for f in items:
                try:
                    d = json.loads(f.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                lines.append(f'  {d.get('id')}  {d.get('what')}')
            lines.append('')
            lines.append('批准：#yes <id>')
            yield event.plain_result('\n'.join(lines))
            return
        if not req.isdigit():
            yield event.plain_result('请求 id 应该是一串数字。')
            return
        src_file = pending / f'{req}.json'
        if not src_file.exists():
            yield event.plain_result(f'找不到请求 {req}，可能已经处理过了。')
            return
        try:
            info = json.loads(src_file.read_text(encoding='utf-8'))
            (pending / f'{req}.approved').write_text('', encoding='utf-8')
        except (OSError, ValueError) as exc:
            yield event.plain_result(f'批准失败：{exc}')
            return
        yield event.plain_result(f'已批准 {req}：{info.get('what')}\n她再跑一次就能执行（凭据只能用一次）。')

    @filter.command('rollback')
    async def cmd_rollback(self, event: AstrMessageEvent, args: GreedyStr):
        """#rollback [list|<快照名>]（主人专用）把插件源码恢复到某个快照。

        Snapshots are taken before every job but previously had no way back:
        the plugin wrote them and never read them. This is that missing half.
        """
        if not self._is_admin(event):
            yield event.plain_result('这个只对主人开放。')
            return
        configured = str(self._c('snapshot_dir', '')).strip()
        root = Path(configured) if configured else SNAPSHOT_ROOT
        snaps = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.is_dir() else []
        if not snaps:
            yield event.plain_result(f'还没有快照。目录：{root}')
            return
        want = str(args).strip()
        if not want or want == 'list':
            lines = [f'快照目录 {root}', f'共 {len(snaps)} 个，最近的在前：']
            for d in snaps[:10]:
                count = sum((1 for _ in d.rglob('*.py')))
                lines.append(f'  {d.name}  ({count} 个 .py)')
            lines.append('')
            lines.append('恢复：#rollback <快照名>（会先比对差异，只覆盖真正不同的文件）')
            yield event.plain_result('\n'.join(lines))
            return
        chosen = next((d for d in snaps if d.name == want), None)
        if chosen is None:
            yield event.plain_result(f'找不到快照 {want}，用「#rollback list」看有哪些。')
            return
        plugins = self._workdir() / 'data' / 'plugins'
        changed: list[Path] = []
        for snap_file in chosen.rglob('*'):
            if not snap_file.is_file():
                continue
            live = plugins / snap_file.relative_to(chosen)
            try:
                if not live.exists() or live.read_bytes() != snap_file.read_bytes():
                    changed.append(snap_file.relative_to(chosen))
            except OSError:
                continue
        if not changed:
            yield event.plain_result(f'{want} 和现在的代码没有差异，不用恢复。')
            return
        safety = root / f'pre-rollback-{time.strftime('%Y%m%d-%H%M%S')}'
        restored, failed = (0, [])
        for rel in changed:
            live = plugins / rel
            try:
                if live.exists():
                    keep = safety / rel
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(live, keep)
                live.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(chosen / rel, live)
                restored += 1
            except OSError as exc:
                failed.append(f'{rel}: {exc}')
        proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'compileall', '-q', str(plugins), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        compiled = proc.returncode == 0
        lines = [f'从 {want} 恢复了 {restored} 个文件。']
        if failed:
            lines.append(f'失败 {len(failed)} 个：{failed[0][:120]}')
        lines.append(f'编译检查：{('通过' if compiled else '失败')}')
        if not compiled:
            lines.append((out or b'').decode(errors='replace')[:300])
        lines.append(f'回滚前的状态存在 {safety.name}，重启 AstrBot 后生效。')
        yield event.plain_result('\n'.join(lines))

    async def _execute(self, question: str) -> tuple[str, list[str]]:
        backend = str(self._c('backend', 'builtin')).strip().lower()
        if backend == 'codex':
            return await self._run_codex(question)
        if backend == 'dsh':
            return await self._run_dsh(question)
        if backend == 'auto':
            try:
                return await self._run_codex(question)
            except Exception as codex_exc:
                self.logger.warning(f'[self_agent] codex failed, falling back to dsh: {codex_exc}')
            try:
                return await self._run_dsh(question)
            except Exception as dsh_exc:
                self.logger.warning(f'[self_agent] dsh failed, falling back to builtin: {dsh_exc}')
                answer, trace = await self._run_builtin(question)
                return (answer, [*trace, f'agent 后端失败回退：{dsh_exc}'[:120]])
        return await self._run_builtin(question)

    async def _run_builtin(self, question: str) -> tuple[str, list[str]]:
        base_url, api_key, model = self._creds()
        if not api_key:
            raise RuntimeError('没有可用的模型凭据，请配置 coder_api_key 或 mem_chat 的 llm_api_key')
        return await agent_core.run_agent(base_url=base_url, api_key=api_key, model=model, temperature=float(self._c('coder_temperature', 0.2)), max_steps=int(self._c('max_steps', 10)), workdir=self._workdir(), user_prompt=question, log=self.logger.info)

    async def _run_codex(self, question: str) -> tuple[str, list[str]]:
        """Run one request through Codex CLI and return its final message.

        Args:
            question: The full prompt, including recent chat and persona context.

        Returns:
            The final assistant message and a short execution trace.

        Raises:
            RuntimeError: If Codex cannot start, times out, fails, or returns no
                final assistant message.
        """
        template = str(self._c('codex_cmd', __import__('share_runtime').resolve('{{AB_SELF_AGENT_CODEX_CMD}}'))).strip()
        base_cmd = shlex.split(template)
        if not base_cmd:
            raise RuntimeError('codex_cmd 为空')
        model = str(self._c('codex_model', 'deepseek-v4.1-flash')).strip()
        if not model:
            raise RuntimeError('codex_model 为空')
        sandbox = str(self._c('codex_sandbox', 'workspace-write')).strip()
        if sandbox not in {'read-only', 'workspace-write', 'danger-full-access'}:
            raise RuntimeError(f'不支持的 Codex sandbox：{sandbox}')
        timeout = int(self._c('timeout_sec', 600))
        env = dict(os.environ)
        env.pop('OPENAI_API_KEY', None)
        if bool(self._c('codex_clear_proxy', True)):
            for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
                env.pop(key, None)
        env['NO_COLOR'] = '1'
        codex_home = str(self._c('codex_home', '')).strip()
        if codex_home:
            env['CODEX_HOME'] = codex_home
        node_bin = str(self._c('node_bin_dir', '')).strip()
        if node_bin:
            env['PATH'] = node_bin + os.pathsep + env.get('PATH', '')
        run_cwd = self._workdir()
        if bool(self._c('protect_plugin_code', True)):
            scratch = Path(str(self._c('agent_workspace', '')).strip() or AGENT_WORKSPACE)
            try:
                scratch.mkdir(parents=True, exist_ok=True)
                run_cwd = scratch
            except OSError as exc:
                self.logger.warning(f'[self_agent] scratch workspace unavailable ({exc}); running Codex in the AstrBot root with plugin code writable')
        cmd = [*base_cmd, 'exec', '--json', '--skip-git-repo-check', '--color', 'never', '-C', str(run_cwd), '-m', model, '-s', sandbox, '-c', 'approval_policy="never"']
        if bool(self._c('codex_ephemeral', True)):
            cmd.append('--ephemeral')
        extra_dirs = self._c('codex_add_dirs', [])
        if isinstance(extra_dirs, str):
            extra_dirs = [x.strip() for x in extra_dirs.split(',') if x.strip()]
        for extra_dir in extra_dirs:
            cmd.extend(['--add-dir', str(extra_dir)])
        if bool(self._c('codex_network_access', True)):
            cmd.extend(['-c', 'sandbox_workspace_write.network_access=true'])
        cmd.append(question)
        run_as = str(self._c('run_as_user', '')).strip()
        if run_as:
            import pwd
            helper = shutil.which(str(self._c('drop_helper', 'drop2aris')))
            try:
                info = pwd.getpwnam(run_as)
            except KeyError:
                self.logger.warning(f"[self_agent] user {run_as} does not exist; running Codex with the bot's own identity")
            else:
                if not helper:
                    self.logger.warning("[self_agent] drop helper not found; Codex keeps the bot's identity and can read its credentials")
                else:
                    cmd = [helper, *cmd]
                    env['HOME'] = info.pw_dir
                    env['USER'] = run_as
                    env['LOGNAME'] = run_as
        self.logger.info(f'[self_agent] codex: {' '.join(cmd[:9])} ... model={model} sandbox={sandbox}')
        started = time.time()
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(run_cwd), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            self.logger.warning(f'[self_agent] codex timeout after {timeout}s, killed')
            raise RuntimeError(f'codex 超时（>{timeout}s）')
        elapsed = time.time() - started
        raw = (out or b'').decode('utf-8', 'replace')
        errt = (err or b'').decode('utf-8', 'replace').strip()
        messages: list[str] = []
        protocol_errors: list[str] = []
        thread_id = ''
        turn_completed = False
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            event_type = str(event.get('type', ''))
            if event_type == 'thread.started':
                thread_id = str(event.get('thread_id', ''))
            elif event_type == 'item.completed':
                item = event.get('item') or {}
                if item.get('type') == 'agent_message':
                    text = str(item.get('text', '')).strip()
                    if text:
                        messages.append(text)
            elif event_type == 'turn.completed':
                turn_completed = True
            elif event_type in ('error', 'turn.failed'):
                message = str(event.get('message') or event)[:400]
                protocol_errors.append(message)
        text = messages[-1] if messages else ''
        self.logger.info(f'[self_agent] codex done exit={proc.returncode} in {elapsed:.1f}s out={len(raw)}b err={len(errt)}b messages={len(messages)} turn_completed={turn_completed} thread={thread_id or '-'}')
        detail = '; '.join(protocol_errors) or errt[-500:]
        if proc.returncode != 0:
            raise RuntimeError(f'codex 退出码 {proc.returncode}：{detail}')
        if not turn_completed:
            raise RuntimeError(f'codex 没有正常结束：{detail}')
        if not text:
            raise RuntimeError(f'codex 没有最终回复：{detail}')
        return (text, [f'codex model={model} exit={proc.returncode} {elapsed:.1f}s'])

    async def _run_dsh(self, question: str) -> tuple[str, list[str]]:
        tmpl = str(self._c('dsh_cmd', 'npx -y @deepseek-ai/dsh@0.2.1-rc.2 headless'))
        cmd = [*shlex.split(tmpl), question]
        timeout = int(self._c('timeout_sec', 240))
        env = dict(os.environ)
        _, api_key, _ = self._creds()
        if api_key:
            env.setdefault('DEEPSEEK_API_KEY', api_key)
        node_bin = str(self._c('node_bin_dir', '')).strip()
        if node_bin:
            env['PATH'] = node_bin + os.pathsep + env.get('PATH', '')
        env['DSH_PERMISSION_MODE'] = str(self._c('dsh_permission_mode', 'workspace-write')).strip() or 'workspace-write'
        run_cwd = self._workdir()
        if bool(self._c('protect_plugin_code', True)):
            scratch = Path(str(self._c('agent_workspace', '')).strip() or AGENT_WORKSPACE)
            try:
                scratch.mkdir(parents=True, exist_ok=True)
                run_cwd = scratch
            except OSError as exc:
                self.logger.warning(f'[self_agent] scratch workspace unavailable ({exc}); running in the AstrBot root with plugin code writable')
        self.logger.info(f'[self_agent] dsh: {' '.join(cmd)}')
        started = time.time()
        run_as = str(self._c('run_as_user', '')).strip()
        if run_as:
            import pwd
            import shutil
            helper = shutil.which(str(self._c('drop_helper', 'drop2aris')))
            try:
                info = pwd.getpwnam(run_as)
            except KeyError:
                self.logger.warning(f"[self_agent] user {run_as} does not exist; running the agent with the bot's own identity")
            else:
                if not helper:
                    self.logger.warning("[self_agent] drop helper not found; the agent keeps the bot's identity and can read its credentials")
                else:
                    cmd = [helper, *cmd]
                    env['HOME'] = info.pw_dir
                    env['USER'] = run_as
                    env['LOGNAME'] = run_as
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(run_cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            self.logger.warning(f'[self_agent] dsh timeout after {timeout}s, killed')
            raise RuntimeError(f'dsh 超时（>{timeout}s）')
        elapsed = time.time() - started
        text = (out or b'').decode('utf-8', 'replace').strip()
        errt = (err or b'').decode('utf-8', 'replace').strip()
        kept = [ln for ln in text.splitlines() if not ln.startswith('dsh:')]
        text = '\n'.join(kept).strip()
        self.logger.info(f'[self_agent] dsh done exit={proc.returncode} in {elapsed:.1f}s out={len(text)}b err={len(errt)}b')
        if proc.returncode != 0 and (not text):
            raise RuntimeError(f'dsh 退出码 {proc.returncode}：{errt[-500:]}')
        if not text:
            raise RuntimeError(f'dsh 没有输出（exit={proc.returncode}）：{errt[-300:]}')
        return (text, [f'dsh exit={proc.returncode}'])
