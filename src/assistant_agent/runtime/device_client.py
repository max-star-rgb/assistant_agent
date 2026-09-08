"""Opt-in local component for the device backend experiment (no Agent loop)."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import shlex
import stat
from urllib.parse import urlsplit

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, validate_call
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from assistant_agent.runtime.device_backend import DeviceReply, DeviceRequest, MAX_FRAME_BYTES
from assistant_agent.runtime.device_files import DeviceFiles


class ShellArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1, max_length=65536)
    timeout: int | None = Field(default=None, ge=1, le=120)


class GitArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_path: str = Field(min_length=1, max_length=4096)
    arguments: list[str] = Field(min_length=1, max_length=128)


async def _execute(command: str, cwd: Path, timeout: int) -> ExecuteResponse:
    """Bounded POSIX subprocess; terminate the process group on timeout/cancel."""
    process = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", command, cwd=cwd,
        env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    output = bytearray()

    async def drain():
        while chunk := await process.stdout.read(8192):
            output.extend(chunk)
            if len(output) > 100_000:
                raise OverflowError
        await process.wait()

    try:
        await asyncio.wait_for(drain(), timeout)
        return ExecuteResponse(output=output.decode("utf-8", errors="replace"), exit_code=process.returncode)
    finally:
        # Also clean up background descendants when the shell itself has exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Drain buffered pipe bytes after killing; wait() alone can deadlock
        # when asyncio has paused reading a full stdout pipe.
        await process.communicate()


async def run_device_client(url: str, token: str, cwd: Path, *, allow_shell: bool = False,
                            host_paths: bool = False, ready: asyncio.Event | None = None,
                            browser_headless: bool = False) -> None:
    """Connect outwards once. Reconnection/replay requires an explicit new session."""
    address = urlsplit(url)
    if (address.scheme not in {"ws", "wss"} or address.username or address.password
            or (address.scheme == "ws" and address.hostname not in {"127.0.0.1", "localhost", "::1"})):
        raise ValueError("use wss for non-loopback device connections")
    if allow_shell and os.name != "posix":
        raise ValueError("experimental shell cancellation requires POSIX")
    if host_paths and address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("host paths require same-host pilot")
    root = cwd.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("cwd must be an existing directory")
    backend = LocalShellBackend(root_dir=root, virtual_mode=not host_paths, inherit_env=False)
    operations = {name: validate_call(getattr(backend, name), config=ConfigDict(strict=True))
                  for name in ("ls", "read", "write", "edit", "delete", "glob", "grep")}
    binary = DeviceFiles(backend)
    from assistant_agent.mcp.browser_runtime import DeviceBrowser
    browser = DeviceBrowser(root, headless=browser_headless)

    scoped_backends = {name: LocalShellBackend(root_dir=root / name, virtual_mode=True, inherit_env=False)
                       for name in ("skills", ".agent/history")}
    scoped_files = {name: DeviceFiles(value) for name, value in scoped_backends.items()}

    def file_operation(request: DeviceRequest):
        scope = request.arguments.pop("_subdirectory", None)
        selected = scoped_backends[scope] if scope else backend
        files = scoped_files[scope] if scope else binary
        if scope and request.operation not in {"ls", "read", "write", "edit", "delete", "glob", "grep", "read_bytes", "write_bytes", "abort_upload"}:
            raise ValueError("invalid scoped operation")
        if request.operation == "read_bytes":
            return files.read(**request.arguments)
        if request.operation == "write_bytes":
            return files.write(**request.arguments)
        if request.operation == "abort_upload":
            return files.abort(**request.arguments)
        if request.operation == "instructions":
            if request.arguments:
                raise ValueError("instructions takes no parameters")
            from assistant_agent.native_agent.assistant_prompt import render_working_directory_section
            return {"text": render_working_directory_section(root)}
        path = request.arguments.get("file_path")
        if request.operation in {"read", "write", "edit"} and isinstance(path, str):
            target = selected._resolve_path(path)
            if not host_paths and not target.is_relative_to(root):
                raise ValueError("path outside approved root")
            if target.exists():
                info = target.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FRAME_BYTES:
                    raise ValueError("file is not a bounded regular file")
                if request.operation == "edit":
                    old = request.arguments.get("old_string")
                    new = request.arguments.get("new_string")
                    if isinstance(old, str) and isinstance(new, str):
                        with target.open("rb") as source:
                            data = source.read(MAX_FRAME_BYTES + 1)
                        content = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                        old = old.replace("\r\n", "\n").replace("\r", "\n")
                        new = new.replace("\r\n", "\n").replace("\r", "\n")
                        count = content.count(old)
                        if not request.arguments.get("replace_all", False):
                            count = min(count, 1)
                        size = len(content.encode("utf-8")) + count * (len(new.encode()) - len(old.encode()))
                        if len(data) > MAX_FRAME_BYTES or size > MAX_FRAME_BYTES:
                            raise ValueError("edited file would exceed size limit")
        if scope:
            return validate_call(getattr(selected, request.operation), config=ConfigDict(strict=True))(**request.arguments)
        return operations[request.operation](**request.arguments)

    def cancel_once(task: asyncio.Task):
        # A second cancellation would interrupt waiting for an in-flight file
        # thread, release the lock, and leave that thread mutating in parallel.
        if not task.cancelling():
            task.cancel()
    active: dict[str, asyncio.Task] = {}
    seen: set[str] = set()
    # ponytail: one operation at a time per device; per-workspace concurrency when needed.
    lock = asyncio.Lock()
    async with connect(url, additional_headers={"Authorization": f"Bearer {token}"},
                       max_size=MAX_FRAME_BYTES, proxy=None) as socket:
        hello = json.loads(await socket.recv())
        if hello.get("type") != "ready":
            raise ValueError("invalid device handshake")
        if ready is not None:
            ready.set()

        async def perform(request: DeviceRequest):
            reply = DeviceReply(request_id=request.request_id)
            try:
                async with lock:
                    if request.operation == "execute":
                        if not allow_shell:
                            reply.error = "shell_disabled"
                        else:
                            args = ShellArguments.model_validate(request.arguments)
                            reply.result = asdict(await _execute(args.command, root, args.timeout or 120))
                    elif request.operation == "git":
                        if not allow_shell:
                            reply.error = "shell_disabled"
                        else:
                            from assistant_agent.tools.git import _overrides_git_repository
                            args = GitArguments(**request.arguments)
                            if _overrides_git_repository(tuple(args.arguments)):
                                raise ValueError("git repository override forbidden")
                            target = await asyncio.to_thread(backend._resolve_path, args.target_path)
                            if await asyncio.to_thread(target.is_file):
                                target = target.parent
                            resolved = await _execute(shlex.join(["git", "-C", str(target), "rev-parse", "--show-toplevel"]), root, 120)
                            repo = resolved.output.strip() if not resolved.exit_code else None
                            result = (await _execute(shlex.join(["git", "-C", repo, *args.arguments]), root, 120)
                                      if repo else resolved)
                            reply.result = {"output": result.output, "exit_code": result.exit_code, "repository_root": repo}
                    elif request.operation == "browser":
                        reply.result = await browser.call(**request.arguments)
                    else:
                        # A synchronous filesystem mutation cannot be undone by task
                        # cancellation; await its completion before releasing the lock.
                        work = asyncio.create_task(asyncio.to_thread(file_operation, request))
                        try:
                            result = await asyncio.shield(work)
                        except asyncio.CancelledError:
                            await work
                            raise
                        if isinstance(result, dict):
                            reply.result = result
                        elif result.error:
                            reply.error = "file_operation_failed"
                        else:
                            reply.result = asdict(result)
            except asyncio.CancelledError:
                reply.error = "cancelled"
            except ValidationError:
                reply.error = "invalid_request"
            except TimeoutError:
                reply.error = "execution_timeout"
            except OverflowError:
                reply.error = "output_limit"
            except (ValueError, OSError, TypeError, KeyError):
                reply.error = "file_operation_failed" if request.operation != "execute" else "operation_failed"
            except Exception:
                reply.error = "operation_failed"
            try:
                payload = reply.model_dump_json()
                if len(payload.encode()) > MAX_FRAME_BYTES:
                    payload = DeviceReply(request_id=request.request_id, error="output_limit").model_dump_json()
                await socket.send(payload)
            except ConnectionClosed:
                pass

        try:
            async for raw in socket:
                try:
                    request = DeviceRequest.model_validate_json(raw)
                except ValidationError:
                    await socket.close(code=1008, reason="invalid_request")
                    break
                if request.type == "cancel":
                    if task := active.get(request.request_id):
                        cancel_once(task)
                    continue
                if request.operation is None:
                    await socket.close(code=1008, reason="invalid_request")
                    break
                if request.request_id in seen:
                    await socket.send(DeviceReply(request_id=request.request_id, error="duplicate_request").model_dump_json())
                    continue
                # Fail closed instead of evicting IDs and making old writes replayable.
                if len(seen) >= 10_000:
                    await socket.close(code=1008, reason="session_request_limit")
                    break
                seen.add(request.request_id)
                if len(active) >= 8:
                    await socket.send(DeviceReply(request_id=request.request_id, error="client_busy").model_dump_json())
                    continue
                task = asyncio.create_task(perform(request))
                active[request.request_id] = task
                task.add_done_callback(lambda done, request_id=request.request_id: active.pop(request_id, None))
        finally:
            pending = list(active.values())
            for task in pending:
                cancel_once(task)
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.to_thread(binary.close)
            for files in scoped_files.values():
                await asyncio.to_thread(files.close)
            await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Experimental local device executor; no model or Agent Server.")
    parser.add_argument("--url", required=True, help="Execution broker WebSocket URL; remote connections require wss")
    parser.add_argument("--token-file", required=True, type=Path, help="Private pairing token file; never sent to the model")
    parser.add_argument("--cwd", required=True, type=Path, help="Locally approved existing directory")
    parser.add_argument("--allow-shell", action="store_true", help="Allow full current OS-user shell access; NOT a directory sandbox")
    args = parser.parse_args()
    asyncio.run(run_device_client(args.url, args.token_file.read_text().strip(), args.cwd, allow_shell=args.allow_shell))


if __name__ == "__main__":
    main()
