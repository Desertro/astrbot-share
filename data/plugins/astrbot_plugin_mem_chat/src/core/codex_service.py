"""Owner-scoped asynchronous Codex tasks backed by App Server threads."""
from __future__ import annotations
import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from astrbot.api import logger
from astrbot.api.star import StarTools
from .codex_app_server import CodexAppServer
_THREAD_RE = re.compile('^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')
_SESSION_NAME_RE = re.compile('[^0-9A-Za-z_\\-\\u4e00-\\u9fff]+')
_ACTIVE = {'starting', 'running', 'stopping'}
DEFAULT_CODEX_CLI = __import__('share_runtime').resolve('{{AB_MEM_CHAT_CODEX_CLI_PATH}}')

def _safe_text(value, limit=8000):
    text = re.sub('\\bsk-[A-Za-z0-9_\\-]{12,}', '[redacted]', str(value or ''))
    text = re.sub('(?i)Bearer\\s+[A-Za-z0-9._\\-]+', 'Bearer [redacted]', text)
    return text[:limit]

class _CodexIdleTimeout(TimeoutError):
    """The current task stopped producing progress for its idle interval."""

class CodexService:
    """Persist task outcomes while keeping chat tools independent of run duration."""

    def __init__(self, config: dict, *, store_path=None, on_complete=None):
        self.config = config or {}
        self.store_path = Path(store_path or self._default_store_path())
        self.on_complete = on_complete
        self._store_lock = asyncio.Lock()
        self._scope_locks = defaultdict(asyncio.Lock)
        self._store = None
        self._runtime = {}
        self._threads = {}
        self._workers = set()
        self._closing = False
        self._client = CodexAppServer(self._cli_path(), self._on_event, self._on_disconnect)

    @staticmethod
    def _default_store_path():
        return StarTools.get_data_dir('mem_chat') / 'codex_sessions.json'

    def _get_conf(self, key, default=None):
        return self.config.get(key, default)

    def _cli_path(self):
        configured = str(self._get_conf('codex_cli_path', '') or '').strip()
        return configured or os.environ.get('CODEX_CLI_PATH') or (DEFAULT_CODEX_CLI if Path(DEFAULT_CODEX_CLI).is_file() else shutil.which('codex') or 'codex')

    def _workdir(self):
        return str(self._get_conf('codex_workdir', __import__('share_runtime').resolve('{{AB_MEM_CHAT_CODEX_WORKDIR}}')) or __import__('share_runtime').resolve('{{AB_MEM_CHAT_CODEX_WORKDIR}}')).strip()

    def _model(self):
        return str(self._get_conf('codex_model', 'deepseek-v4.1-flash') or '').strip()

    def _sandbox(self):
        value = self._get_conf('codex_sandbox', 'workspace-write')
        return value if value in {'read-only', 'workspace-write', 'danger-full-access'} else 'workspace-write'

    def _timeout_sec(self):
        """Read the idle interval, accepting the legacy timeout setting.

        Returns:
            Positive idle seconds; this never limits total task duration.
        """
        value = self._get_conf('codex_idle_timeout_sec')
        if value in (None, '', 0, '0'):
            value = self._get_conf('codex_timeout_sec', 600)
        try:
            return max(15.0, float(value or 600))
        except (TypeError, ValueError):
            return 600.0

    def _allowed_uids(self):
        allowed = self._get_conf('codex_allowed_uids') or self._get_conf('private_chat_uids', []) or []
        if isinstance(allowed, str):
            allowed = re.split('[,;\\s]+', allowed)
        return {str(value).strip() for value in allowed}

    def is_authorized(self, event):
        sender = str(getattr(event, 'get_sender_id', lambda: '')() or '')
        group = bool(getattr(event, 'get_group_id', lambda: '')())
        return sender in self._allowed_uids() and (not group or self._get_conf('codex_allow_groups', False))

    @staticmethod
    def _scope_id(event):
        return str(getattr(event, 'unified_msg_origin', '') or '').strip()

    async def _load_store(self):
        if self._store is None:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text(encoding='utf-8-sig'))
                if not isinstance(data.get('scopes'), dict):
                    raise ValueError('Invalid Codex session store')
            else:
                data = {'scopes': {}}
            data['version'] = 2
            data.setdefault('runs', {})
            for run in data['runs'].values():
                if run.get('status') in _ACTIVE:
                    run.update(status='unknown', finished_at=time.time(), error='服务已重启，旧任务的执行状态未知；没有自动重跑，请先查结果再决定是否继续。')
            self._store = data
        return self._store

    async def _save_store(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(self._store, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.store_path)

    async def _resolve_session(self, scope, *, thread_id='', session_name=''):
        data = await self._load_store()
        group = data['scopes'].get(scope, {})
        sessions = group.get('sessions', {})
        if thread_id:
            if not _THREAD_RE.fullmatch(thread_id):
                return {'error': 'thread_id 必须是 Codex 的 UUID。'}
            return sessions.get(thread_id) or {'error': '该线程没有绑定到当前聊天。'}
        if session_name:
            matches = [s for s in sessions.values() if s.get('name') == session_name]
            return matches[0] if len(matches) == 1 else {'error': '未找到唯一匹配的会话，请用 list 查看后指定 thread_id。'}
        return sessions.get(group.get('last_thread_id')) or {'error': '当前聊天还没有 Codex 会话，请先新建任务。'}

    async def _resolve_run(self, scope, *, task_id='', thread_id='', session_name=''):
        data = await self._load_store()
        if task_id:
            run = data['runs'].get(task_id)
            return run if run and run.get('scope') == scope else None
        group = data['scopes'].get(scope, {})
        if thread_id or session_name:
            matches = [r for r in data['runs'].values() if r['scope'] == scope and (r.get('thread_id') == thread_id if thread_id else r.get('session_name') == session_name)]
            return matches[-1] if matches else None
        return data['runs'].get(group.get('last_task_id'))

    @staticmethod
    def _public_run(run):
        return {key: run.get(key) for key in ('task_id', 'status', 'session_name', 'thread_id', 'turn_id', 'model', 'cwd', 'progress', 'idle_timeout_sec', 'last_activity_at', 'result', 'error', 'created_at', 'finished_at', 'delivery') if run.get(key) is not None}

    def _on_disconnect(self, exc):
        for runtime in list(self._runtime.values()):
            if not runtime['done'].done():
                runtime['done'].set_exception(ConnectionError('Codex 连接中断；任务没有自动重跑。'))

    def _on_event(self, method, params):
        """Fold public progress and terminal events, including pre-ack notifications.

        Args:
            method: Native notification name.
            params: Native event fields.
        """
        runtime = self._threads.get(params.get('threadId'))
        if not runtime or runtime['done'].done():
            return
        run = runtime['run']
        turn = params.get('turn') or {}
        turn_id = params.get('turnId') or turn.get('id')
        if turn_id:
            if run.get('turn_id') and turn_id != run['turn_id']:
                return
            run['turn_id'] = turn_id
        if method in {'turn/started', 'turn/completed', 'turn/diff/updated', 'turn/plan/updated', 'item/started', 'item/completed', 'item/mcpToolCall/progress', 'item/reasoning/summaryPartAdded'} or (method.startswith('item/') and params.get('delta')):
            runtime['last_activity'] = time.monotonic()
            run['last_activity_at'] = time.time()
        if method == 'turn/started':
            run['progress'] = 'Codex 正在处理任务'
            run['status'] = 'stopping' if runtime['stop_requested'] else 'running'
        elif method in {'item/started', 'item/completed'}:
            item = params.get('item') or {}
            kind = item.get('type')
            if kind == 'agentMessage':
                text = _safe_text(item.get('text'))
                if text:
                    run['progress'] = text[:1000]
                    if method == 'item/completed' and item.get('phase') != 'commentary':
                        phase = item.get('phase') or 'unphased'
                        runtime['answers'][phase] = text
            elif kind in {'commandExecution', 'fileChange', 'mcpToolCall', 'webSearch'}:
                labels = {'commandExecution': '执行命令', 'fileChange': '修改文件', 'mcpToolCall': '调用工具', 'webSearch': '搜索资料'}
                detail = item.get('command') or item.get('tool') or ''
                run['progress'] = _safe_text(f'{labels[kind]}：{detail}', 1000)
        elif method == 'alice/interactionUnavailable':
            run['progress'] = 'Codex 请求交互或额外权限，桥接没有批准。'
        elif method == 'turn/completed':
            for item in turn.get('items', []):
                if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                    runtime['answers'][item.get('phase') or 'unphased'] = _safe_text(item.get('text'))
            runtime['done'].set_result(turn)

    async def _drive_task(self, runtime, prompt, is_new):
        """Create or restore the durable thread, then wait for its actual terminal event."""
        run = runtime['run']
        await self._client.start()
        runtime['last_activity'] = time.monotonic()
        run['last_activity_at'] = time.time()
        if runtime['stop_requested']:
            return {'status': 'interrupted'}
        params = {'cwd': run['cwd'], 'approvalPolicy': 'never', 'sandbox': self._sandbox()}
        if self._model():
            params['model'] = self._model()
        if not is_new:
            params.update(threadId=run['thread_id'], excludeTurns=True)
        response = await self._client.request('thread/start' if is_new else 'thread/resume', params)
        thread = response.get('thread') or {}
        thread_id = str(thread.get('id') or '')
        if not _THREAD_RE.fullmatch(thread_id) or (not is_new and thread_id != run['thread_id']):
            raise RuntimeError('Codex 返回的线程与请求不匹配。')
        run['thread_id'] = thread_id
        runtime['last_activity'] = time.monotonic()
        run['last_activity_at'] = time.time()
        self._threads[thread_id] = runtime
        async with self._store_lock:
            data = await self._load_store()
            scope = data['scopes'][run['scope']]
            old = scope['sessions'].get(thread_id, {})
            scope['sessions'][thread_id] = {'thread_id': thread_id, 'name': run['session_name'], 'cwd': run['cwd'], 'created_at': old.get('created_at', time.time()), 'last_used_at': time.time(), 'last_task_id': run['task_id']}
            if scope.get('last_task_id') == run['task_id']:
                scope['last_thread_id'] = thread_id
            await self._save_store()
        if runtime['stop_requested']:
            return {'status': 'interrupted'}
        if runtime['queued']:
            prompt += '\n\n用户追加要求：\n' + '\n'.join(runtime['queued'])
            runtime['queued'].clear()
        response = await self._client.request('turn/start', {'threadId': thread_id, 'input': [{'type': 'text', 'text': prompt}], 'model': self._model() or None, 'approvalPolicy': 'never'})
        turn_id = str((response.get('turn') or {}).get('id') or '')
        if not turn_id or (run.get('turn_id') and run['turn_id'] != turn_id):
            raise RuntimeError('Codex 返回的轮次与请求不匹配。')
        run['turn_id'] = turn_id
        if not runtime['done'].done():
            run['status'] = 'stopping' if runtime['stop_requested'] else 'running'
        runtime['ready'] = True
        async with self._store_lock:
            await self._save_store()
        if runtime['stop_requested'] and (not runtime['done'].done()):
            await self._client.request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})
        elif runtime['queued'] and (not runtime['done'].done()):
            queued = '\n'.join(runtime['queued'])
            runtime['queued'].clear()
            await self._client.request('turn/steer', {'threadId': thread_id, 'expectedTurnId': turn_id, 'input': [{'type': 'text', 'text': queued}]})
        return await asyncio.shield(runtime['done'])

    async def _run_task(self, runtime, prompt, is_new):
        """Watch task inactivity and persist outcomes without replaying work."""
        run = runtime['run']
        driver = asyncio.create_task(self._drive_task(runtime, prompt, is_new))
        try:
            while not driver.done():
                remaining = run['idle_timeout_sec'] - (time.monotonic() - runtime['last_activity'])
                if remaining <= 0:
                    raise _CodexIdleTimeout
                await asyncio.wait({driver}, timeout=remaining)
            turn = await driver
            status = turn.get('status')
            answers = runtime['answers']
            result = answers.get('final_answer') or answers.get('partial_answer') or answers.get('unphased') or ''
            run['result'] = result
            if status == 'completed' and result:
                run['status'] = 'completed'
            elif status == 'interrupted':
                run['status'] = 'interrupted'
            else:
                run.update(status='failed', error=_safe_text((turn.get('error') or {}).get('message') or 'Codex 未返回可用的最终结果。'))
        except _CodexIdleTimeout:
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
            idle_error = f'Codex 连续 {run['idle_timeout_sec']:g} 秒没有任务进展（空闲超时）'
            logger.warning('[mem_chat] Codex idle timeout task=%s idle_timeout_sec=%s', run['task_id'], run['idle_timeout_sec'])
            run.update(status='stopping', error=idle_error + '，已请求中断。')
            try:
                if not run.get('turn_id'):
                    raise RuntimeError('Codex 尚未确认轮次')
                await self._client.request('turn/interrupt', {'threadId': run['thread_id'], 'turnId': run['turn_id']}, timeout=5)
                await asyncio.wait_for(asyncio.shield(runtime['done']), timeout=10)
                run.update(status='timeout', error=idle_error + '，当前轮次已结束；可以查看状态后继续。')
            except Exception:
                await self._client.close()
                self._on_disconnect(ConnectionError('Server closed after unconfirmed idle timeout'))
                run.update(status='unknown', error=idle_error + '；未收到停止确认，已关闭桥接进程，请核对实际产物，未自动重跑。')
        except asyncio.TimeoutError:
            run.update(status='unknown' if run.get('thread_id') else 'failed', error='Codex 单次启动或控制请求超时；未自动重跑，请核对会话状态。')
        except asyncio.CancelledError:
            run.update(status='unknown', error='插件已卸载，任务已请求停止；请核对实际产物后再继续。')
            raise
        except Exception as exc:
            run.update(status='failed', error=_safe_text(exc, 1500))
        finally:
            if not driver.done():
                driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
            run['finished_at'] = time.time()
            if self._threads.get(run.get('thread_id')) is runtime:
                self._threads.pop(run['thread_id'], None)
            if runtime['done'].done() and (not runtime['done'].cancelled()):
                runtime['done'].exception()
            async with self._store_lock:
                await self._save_store()
            logger.info('[mem_chat] Codex task finished task=%s status=%s', run['task_id'], run['status'])
        if self.on_complete and (not self._closing):
            try:
                delivered = await self.on_complete(run['scope'], self._public_run(run))
                run['delivery'] = 'sent' if delivered else 'pending'
            except Exception as exc:
                run['delivery'] = 'pending'
                logger.warning('[mem_chat] Codex result delivery failed task=%s error=%s', run['task_id'], type(exc).__name__)
            async with self._store_lock:
                await self._save_store()
        self._runtime.pop(run['task_id'], None)

    async def execute_tool(self, event, *, action='resume', prompt='', thread_id='', session_name='', task_id=''):
        """Authorize each operation and return quickly while native work runs in background.

        Args:
            event: Original authorized chat event.
            action: new, resume, list, status, steer, or stop.
            prompt: Task text or a follow-up instruction.
            thread_id: Explicit thread registered to this chat.
            session_name: Registered session name.
            task_id: Explicit task registered to this chat.

        Returns:
            JSON describing acceptance, progress, or a confirmed terminal outcome.
        """
        if not self._get_conf('codex_enabled', True) or self._closing:
            return 'Codex 工具当前未启用。'
        if not self.is_authorized(event):
            return 'Codex 工具仅允许配置中的 owner 在允许的聊天中使用。'
        scope = self._scope_id(event)
        if not scope:
            return '无法确定当前聊天，Codex 调用已取消。'
        action = str(action or 'resume').strip().lower()
        action = {'start': 'new', 'create': 'new', 'continue': 'resume', 'reply': 'resume', 'cancel': 'stop'}.get(action, action)
        if action not in {'new', 'resume', 'list', 'status', 'steer', 'stop'}:
            return 'action 只能是 new、resume、list、status、steer 或 stop。'
        if sum((bool(v) for v in (task_id, thread_id, session_name))) > 1:
            return 'task_id、thread_id 和 session_name 只需指定其中一个，避免目标冲突。'
        if action == 'new' and (task_id or thread_id):
            return '新任务不要指定已有 task_id 或 thread_id；继续已有任务请使用 resume。'
        prompt = str(prompt or '').strip()
        if action in {'new', 'resume', 'steer'} and (not 0 < len(prompt) <= 12000):
            return 'prompt 不能为空，且不能超过 12000 字符。'
        async with self._scope_locks[scope]:
            async with self._store_lock:
                data = await self._load_store()
                group = data['scopes'].get(scope, {})
                if action == 'list':
                    return json.dumps({'sessions': list(group.get('sessions', {}).values()), 'tasks': [self._public_run(r) for r in list(data['runs'].values()) if r['scope'] == scope][-10:]}, ensure_ascii=False)
                run = await self._resolve_run(scope, task_id=task_id, thread_id=thread_id, session_name=session_name)
            if action in {'status', 'steer', 'stop'} or (action == 'resume' and run and (run['status'] in _ACTIVE)):
                if not run:
                    return '未找到当前聊天的对应任务，请先 list 查看。'
                runtime = self._runtime.get(run['task_id'])
                if action == 'status':
                    return json.dumps(self._public_run(run), ensure_ascii=False)
                if run['status'] not in _ACTIVE or not runtime:
                    return json.dumps({**self._public_run(run), 'note': '该任务已结束；追加工作请使用 resume。'}, ensure_ascii=False)
                if action == 'stop':
                    runtime['stop_requested'] = True
                    run['status'] = 'stopping'
                    if runtime['ready'] and (not runtime['done'].done()):
                        await self._client.request('turn/interrupt', {'threadId': run['thread_id'], 'turnId': run['turn_id']}, timeout=5)
                    note = '已请求停止，尚需等待 Codex 确认中断；不要宣称已经停止。'
                else:
                    if runtime['stop_requested']:
                        return '任务正在停止，请等确认结束后再 resume。'
                    if not runtime['ready']:
                        runtime['queued'].append(prompt)
                        note = '追加要求已排队，将在任务启动时传入；尚未完成。'
                    else:
                        await self._client.request('turn/steer', {'threadId': run['thread_id'], 'expectedTurnId': run['turn_id'], 'input': [{'type': 'text', 'text': prompt}]}, timeout=5)
                        note = '追加要求已送入当前执行轮次；尚未完成。'
                async with self._store_lock:
                    await self._save_store()
                return json.dumps({**self._public_run(run), 'note': note}, ensure_ascii=False)
            if action == 'new' and session_name and (any((s.get('name') == session_name for s in group.get('sessions', {}).values())) or any((r['scope'] == scope and r.get('session_name') == session_name and (r['status'] in _ACTIVE) for r in data['runs'].values()))):
                return '当前聊天已有同名 Codex 会话，请用 resume 继续，或选择其他会话名。'
            target = None
            if action == 'resume':
                if task_id and (not run):
                    return '该任务没有绑定到当前聊天。'
                if run is not None and (not run.get('thread_id')):
                    return '该任务没有建立 Codex 线程，不能续聊；请先检查失败原因再新建任务。'
                async with self._store_lock:
                    target = await self._resolve_session(scope, thread_id=(run.get('thread_id') if run and (not (thread_id or session_name)) else thread_id) or '', session_name=session_name)
                if target.get('error'):
                    return target['error']
                for active in self._runtime.values():
                    if active['run'].get('thread_id') == target['thread_id'] and active['run']['status'] in _ACTIVE:
                        return '该会话已有任务在执行，请用 steer 补充要求或 stop 停止。'
            name = _SESSION_NAME_RE.sub('-', session_name or (target or {}).get('name', '')).strip('-')[:48]
            new_id = uuid.uuid4().hex[:12]
            name = name or 'alice-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + new_id[:4]
            run = {'task_id': new_id, 'scope': scope, 'status': 'starting', 'session_name': name, 'thread_id': (target or {}).get('thread_id', ''), 'cwd': (target or {}).get('cwd') or self._workdir(), 'model': self._model(), 'created_at': time.time(), 'idle_timeout_sec': self._timeout_sec(), 'last_activity_at': time.time(), 'progress': '正在启动 Codex', 'delivery': 'pending'}
            runtime = {'run': run, 'done': asyncio.get_running_loop().create_future(), 'answers': {}, 'ready': False, 'queued': [], 'stop_requested': False, 'last_activity': time.monotonic()}
            async with self._store_lock:
                data['runs'][new_id] = run
                group = data['scopes'].setdefault(scope, {'sessions': {}})
                group['last_task_id'] = new_id
                await self._save_store()
            self._runtime[new_id] = runtime
            worker = asyncio.create_task(self._run_task(runtime, prompt, action == 'new'))
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)
            logger.info('[mem_chat] Codex task accepted task=%s action=%s', new_id, action)
            return json.dumps({**self._public_run(run), 'note': '任务已受理，后台执行，尚未完成；结束后会另行通知。'}, ensure_ascii=False)

    async def close(self):
        """Request interruption, then close only this bridge's owned runtime."""
        self._closing = True
        for runtime in list(self._runtime.values()):
            run = runtime['run']
            if run['status'] in _ACTIVE and run.get('turn_id'):
                try:
                    await self._client.request('turn/interrupt', {'threadId': run['thread_id'], 'turnId': run['turn_id']}, timeout=2)
                except Exception:
                    pass
        workers = list(self._workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await self._client.close()
        self._runtime.clear()
        self._threads.clear()
