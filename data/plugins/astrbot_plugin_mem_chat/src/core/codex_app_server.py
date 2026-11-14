"""Bounded JSON-RPC transport for a local Codex App Server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path


class CodexAppServer:
    """Own one stdio server, its request futures, and its process lifetime."""

    def __init__(self, cli_path: str, on_event: Callable, on_disconnect: Callable):
        self.cli_path = cli_path
        self.on_event = on_event
        self.on_disconnect = on_disconnect
        self.process = None
        self._reader = None
        self._stderr_reader = None
        self._pending = {}
        self._sequence = 0
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._ready = False
        self._stderr_tail = bytearray()

    async def _launch_environment(self):
        """Find a live WSL interop endpoint before starting any native task.

        Returns:
            A child-only environment with a verified Windows launch channel.

        Raises:
            ConnectionError: No available WSL endpoint can launch the CLI.
        """
        env = dict(os.environ)
        env.setdefault("NO_COLOR", "1")
        if (
            os.name == "nt"
            or not self.cli_path.lower().endswith(".exe")
            or not env.get("WSL_INTEROP")
        ):
            return env
        directory = Path("/run/WSL")
        candidates = [env["WSL_INTEROP"], str(directory / "1_interop")]
        entries = []
        for path in directory.glob("*_interop"):
            try:
                info = path.stat()
            except OSError:
                # Interactive WSL endpoints can disappear during discovery.
                continue
            if stat.S_ISSOCK(info.st_mode):
                entries.append((info.st_mtime, str(path)))
        candidates.extend(path for _, path in sorted(entries, reverse=True)[:8])
        for endpoint in dict.fromkeys(candidates):
            if not Path(endpoint).is_socket():
                continue
            env["WSL_INTEROP"] = endpoint
            probe = None
            try:
                probe = await asyncio.create_subprocess_exec(
                    self.cli_path,
                    "--version",
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await asyncio.wait_for(probe.wait(), timeout=2) == 0:
                    return env
            except (OSError, asyncio.TimeoutError):
                pass
            finally:
                if probe is not None and probe.returncode is None:
                    probe.kill()
                    await probe.wait()
        raise ConnectionError(
            "Codex could not start: no working WSL Windows interop endpoint"
        )

    async def start(self):
        """Start lazily and finish initialization before accepting requests."""
        async with self._start_lock:
            if self._ready and self.process and self.process.returncode is None:
                return
            await self.close()
            self._closing = False
            overrides = []
            # Read native routing, then disable personal MCP servers only for this
            # owned process. Empty TOML tables merge, so each server needs a flag.
            try:
                env = await self._launch_environment()
                for attempt in range(2):
                    self._stderr_tail.clear()
                    self.process = await asyncio.create_subprocess_exec(
                        self.cli_path,
                        "app-server",
                        "--stdio",
                        "-c",
                        'approval_policy="never"',
                        *overrides,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        limit=4 * 1024 * 1024,
                    )
                    self._reader = asyncio.create_task(self._read_messages())
                    self._stderr_reader = asyncio.create_task(self._drain_stderr())
                    await self.request(
                        "initialize",
                        {"clientInfo": {"name": "alice_mem_chat", "version": "1.0.0"}},
                        timeout=30,
                    )
                    await self._write({"method": "initialized"})
                    native = await self.request("config/read", {"includeLayers": False})
                    servers = native.get("config", {}).get("mcp_servers", {}) or {}
                    enabled = [
                        name
                        for name, conf in servers.items()
                        if conf.get("enabled", True)
                    ]
                    windows = native.get("config", {}).get("windows") or {}
                    needs_windows_sandbox = (
                        os.name == "nt" or self.cli_path.lower().endswith(".exe")
                    ) and not windows.get("sandbox")
                    if not enabled and not needs_windows_sandbox:
                        self._ready = True
                        break
                    if attempt:
                        raise RuntimeError(
                            "Could not initialize isolated Codex sandbox configuration"
                        )
                    for name in enabled:
                        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                            raise RuntimeError(
                                "Unsupported MCP server name for process isolation"
                            )
                        overrides.extend(["-c", f"mcp_servers.{name}.enabled=false"])
                    if needs_windows_sandbox:
                        # No UAC setup or access bypass: enable the native restricted
                        # token sandbox when the CLI has no implementation selected.
                        overrides.extend(["-c", 'windows.sandbox="unelevated"'])
                    await self.close()
                    self._closing = False
            except BaseException:
                await self.close()
                raise

    async def _write(self, message: dict):
        async with self._write_lock:
            if not self.process or self.process.returncode is not None:
                raise ConnectionError("Codex App Server is not running")
            self.process.stdin.write(
                (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            )
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict, *, timeout=15):
        """Send a request with a deadline and correlate out-of-order responses.

        Args:
            method: Native App Server method.
            params: JSON request parameters.
            timeout: Maximum time to wait for its acknowledgement.

        Returns:
            The native result object.
        """
        self._sequence += 1
        request_id = self._sequence
        pending = asyncio.get_running_loop().create_future()
        self._pending[request_id] = pending
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(pending, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
            if not pending.done():
                pending.cancel()

    async def _read_messages(self):
        """Route replies and notifications without blocking on a task result."""
        try:
            while raw := await self.process.stdout.readline():
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    method = message["method"]
                    params = message.get("params") or {}
                    if "id" in message:
                        # This bridge never grants additional interactive permissions.
                        if method in {
                            "item/commandExecution/requestApproval",
                            "item/fileChange/requestApproval",
                        }:
                            result = {"decision": "decline"}
                        elif method == "item/permissions/requestApproval":
                            result = {"permissions": {}, "scope": "turn"}
                        elif method == "item/tool/requestUserInput":
                            result = {"answers": {}}
                        elif method == "mcpServer/elicitation/request":
                            result = {"action": "decline", "content": None}
                        else:
                            await self._write(
                                {
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "Interactive request unavailable in Alice bridge",
                                    },
                                }
                            )
                            self.on_event("alice/interactionUnavailable", params)
                            continue
                        await self._write({"id": message["id"], "result": result})
                        self.on_event("alice/interactionUnavailable", params)
                    else:
                        self.on_event(method, params)
                    continue
                pending = self._pending.get(message.get("id"))
                if pending is not None and not pending.done():
                    if "error" in message:
                        error = message["error"]
                        pending.set_exception(
                            RuntimeError(str(error.get("message", error)))
                        )
                    else:
                        pending.set_result(message.get("result") or {})
            # stderr can reach EOF just after stdout; retain a bounded diagnostic.
            if self._stderr_reader:
                try:
                    await asyncio.wait_for(asyncio.shield(self._stderr_reader), 0.2)
                except asyncio.TimeoutError:
                    pass
            detail = self._stderr_tail.decode("utf-8", errors="replace")
            detail = re.sub(r"\x1b\[[0-9;]*m", "", detail)
            detail = re.sub(
                r"(?i)(?:sk-[A-Za-z0-9_-]+|bearer\s+[^\s]+|"
                r"(?:api[_-]?key|authorization|token|password)\s*[=:]\s*[^\s,;]+)",
                "[redacted]",
                detail,
            )
            detail = " ".join(detail.split())[-1000:]
            raise ConnectionError(
                "Codex App Server disconnected" + (f": {detail}" if detail else "")
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ready = False
            for pending in list(self._pending.values()):
                if not pending.done():
                    pending.set_exception(ConnectionError(str(exc)))
            if not self._closing:
                self.on_disconnect(exc)

    async def _drain_stderr(self):
        # Drain in chunks so verbose diagnostics cannot deadlock stdout processing.
        while chunk := await self.process.stderr.read(8192):
            self._stderr_tail.extend(chunk)
            del self._stderr_tail[:-8192]

    async def close(self):
        """Close pipes and reap the owned server, including on plugin reload."""
        self._closing = True
        self._ready = False
        process = self.process
        if process and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader, self._stderr_reader):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._reader, self._stderr_reader) if t),
            return_exceptions=True,
        )
        for pending in list(self._pending.values()):
            if not pending.done():
                pending.set_exception(ConnectionError("Codex App Server closed"))
        self._pending.clear()
        self.process = None
