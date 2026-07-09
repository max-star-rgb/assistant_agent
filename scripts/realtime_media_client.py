#!/usr/bin/env python3
"""Media Relay protocol smoke client for `/ws/realtime/media`."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


SCENARIOS = ("basic", "ping", "cancel", "hangup", "all")
DEFAULT_TEXT = "Reply exactly REAL_LLM_OK and do not call tools."
REPO_ROOT = Path(__file__).resolve().parents[1]
TRACE_VIEW_SCRIPT = REPO_ROOT / "scripts" / "trace_view.py"


class MediaSmokeError(RuntimeError):
    """Raised when the media protocol smoke detects an invalid frame sequence."""


@dataclass
class ScenarioResult:
    name: str
    session_id: str
    run_id: str | None = None
    response_text: str = ""
    terminal_reason: str | None = None


@dataclass(frozen=True)
class OperatorCommand:
    kind: str
    text: str = ""
    interrupt: bool = False
    should_exit: bool = False


@dataclass
class OperatorSessionState:
    session_id: str
    log_path: Path | None = None
    sent_count: int = 0
    recv_count: int = 0
    turns: int = 0
    completed: int = 0
    cancelled: int = 0
    failed: int = 0
    errors: int = 0
    active_run_id: str | None = None
    last_run_id: str | None = None
    last_trace_id: str | None = None
    assistant_line_open: bool = False
    assistant_chunks: list[str] = field(default_factory=list)
    frame_counts: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def assistant_text(self) -> str:
        return "".join(self.assistant_chunks)

    def record_send(self, event: dict[str, Any]) -> None:
        self.sent_count += 1
        self._write_jsonl("send", event)

    def record_recv(self, frame: dict[str, Any]) -> None:
        self.recv_count += 1
        frame_type = str(frame.get("type") or "")
        self.frame_counts[frame_type] = self.frame_counts.get(frame_type, 0) + 1
        if frame_type == "run.started":
            self.turns += 1
            self.active_run_id = _optional_string(frame.get("run_id"))
            self.last_run_id = self.active_run_id or self.last_run_id
        elif frame_type == "stream.chunk":
            text = _chunk_text(frame)
            if text:
                self.assistant_chunks.append(text)
        elif frame_type == "run.end":
            self._record_run_end(frame)
        elif frame_type == "error":
            self.errors += 1
        self._write_jsonl("recv", frame)

    def report(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": self.turns,
            "completed": self.completed,
            "cancelled": self.cancelled,
            "failed": self.failed,
            "errors": self.errors,
            "sent": self.sent_count,
            "received": self.recv_count,
            "last_run_id": self.last_run_id,
            "active_run_id": self.active_run_id,
            "last_trace_id": self.last_trace_id,
            "assistant_text": self.assistant_text,
            "log_path": str(self.log_path) if self.log_path else None,
            "frame_counts": dict(sorted(self.frame_counts.items())),
        }

    def _record_run_end(self, frame: dict[str, Any]) -> None:
        self.last_run_id = _optional_string(frame.get("run_id")) or self.last_run_id
        reason = _optional_string(frame.get("reason"))
        if reason == "completed":
            self.completed += 1
        elif reason == "cancelled":
            self.cancelled += 1
        else:
            self.failed += 1
        if self.active_run_id == self.last_run_id:
            self.active_run_id = None
        payload = frame.get("payload")
        if isinstance(payload, dict):
            self.last_trace_id = _optional_string(payload.get("trace_id")) or self.last_trace_id

    def _write_jsonl(self, direction: str, message: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "direction": direction,
            "type": str(message.get("type") or ""),
            "message": message,
        }
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Media Relay protocol smoke scenarios against /ws/realtime/media."
    )
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="HTTP server base URL.")
    parser.add_argument("--scenario", choices=SCENARIOS, default="basic")
    parser.add_argument("--user-id", default="media_smoke_user")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--strict-cancel",
        action="store_true",
        help="Require cancel/hangup scenarios to end with reason=cancelled.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run a manual text realtime call operator against /ws/realtime/media.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="Directory for interactive operator JSONL frame logs.",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print final scenario summaries.")
    return parser


async def run_media_smoke(
    *,
    server: str,
    scenario: str,
    user_id: str,
    session_id: str | None,
    text: str,
    timeout: float,
    strict_cancel: bool,
    quiet: bool,
) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional operator dependency.
        raise RuntimeError("Install websockets to use scripts/realtime_media_client.py") from exc

    selected = _selected_scenarios(scenario)
    results: list[ScenarioResult] = []
    for name in selected:
        sid = session_id if len(selected) == 1 and session_id else f"media-smoke-{name}-{uuid.uuid4()}"
        url = build_media_ws_url(server, user_id=user_id, session_id=sid)
        try:
            async with websockets.connect(url, open_timeout=min(10.0, timeout), close_timeout=2.0) as ws:
                result = await _run_one_scenario(
                    ws=ws,
                    name=name,
                    user_id=user_id,
                    session_id=sid,
                    text=text,
                    timeout=timeout,
                    strict_cancel=strict_cancel,
                    quiet=quiet,
                )
                results.append(result)
        except MediaSmokeError as exc:
            print(f"[failed] {name}: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"[error] {name}: {exc}", file=sys.stderr)
            return 2

    for result in results:
        print(
            json.dumps(
                {
                    "scenario": result.name,
                    "session_id": result.session_id,
                    "run_id": result.run_id,
                    "terminal_reason": result.terminal_reason,
                    "response_text": result.response_text,
                },
                ensure_ascii=False,
            )
        )
    return 0


def build_media_ws_url(
    server: str,
    *,
    user_id: str,
    session_id: str,
    client: str = "media_service",
) -> str:
    base = server.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    elif not base.startswith(("ws://", "wss://")):
        raise MediaSmokeError("server must start with http://, https://, ws://, or wss://")
    query = urlencode({"user_id": user_id, "session_id": session_id, "client": client})
    return f"{base}/ws/realtime/media?{query}"


def session_start_event(*, user_id: str, session_id: str) -> dict[str, Any]:
    return {
        "type": "session.start",
        "session_id": session_id,
        "user_id": user_id,
        "payload": {
            "session_id": session_id,
            "config": {
                "entry": "scripted_media_relay",
                "identity_bound": True,
                "locale": "zh-CN",
            },
        },
    }


def transcript_final_event(
    *,
    user_id: str,
    session_id: str,
    text: str,
    interrupt: bool = False,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": "scripted_media_relay",
        "transport": "websocket",
    }
    payload: dict[str, Any] = {
        "text": text,
        "metadata": metadata,
    }
    if interrupt:
        payload["interrupt"] = True
    return {
        "type": "transcript.final",
        "session_id": session_id,
        "user_id": user_id,
        "payload": payload,
    }


def run_cancel_event(*, user_id: str, session_id: str, run_id: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "run.cancel",
        "session_id": session_id,
        "user_id": user_id,
    }
    if run_id:
        event["run_id"] = run_id
    return event


def session_end_event(*, user_id: str, session_id: str, reason: str = "scripted_client_end") -> dict[str, Any]:
    return {
        "type": "session.end",
        "session_id": session_id,
        "user_id": user_id,
        "payload": {"reason": reason},
    }


def ping_event(*, user_id: str, session_id: str) -> dict[str, Any]:
    return {"type": "ping", "session_id": session_id, "user_id": user_id}


def parse_operator_command(line: str) -> OperatorCommand:
    stripped = line.strip()
    if not stripped:
        return OperatorCommand(kind="noop")
    if not stripped.startswith("/"):
        return OperatorCommand(kind="transcript", text=stripped)

    command, _, rest = stripped.partition(" ")
    command = command.lower()
    rest = rest.strip()
    if command in {"/interrupt", "/barge-in", "/barge_in"}:
        if not rest:
            return OperatorCommand(kind="invalid", text="/interrupt requires text")
        return OperatorCommand(kind="transcript", text=rest, interrupt=True)
    if command == "/cancel":
        return OperatorCommand(kind="cancel")
    if command == "/hangup":
        return OperatorCommand(kind="hangup", should_exit=True)
    if command in {"/quit", "/exit"}:
        return OperatorCommand(kind="hangup", should_exit=True)
    if command == "/ping":
        return OperatorCommand(kind="ping")
    if command == "/report":
        return OperatorCommand(kind="report")
    if command == "/help":
        return OperatorCommand(kind="help")
    if command == "/trace" and rest == "last":
        return OperatorCommand(kind="trace_last")
    return OperatorCommand(kind="invalid", text=f"unknown operator command: {stripped}")


def format_trace_view_command(trace_id: str, *, server: str) -> str:
    return shlex.join(
        [
            sys.executable,
            "scripts/trace_view.py",
            trace_id,
            "--server",
            server,
        ]
    )


def format_operator_report(report: dict[str, Any]) -> str:
    log_part = f" log={report['log_path']}" if report.get("log_path") else ""
    trace_part = f" last_trace={report['last_trace_id']}" if report.get("last_trace_id") else ""
    active_part = f" active={report['active_run_id']}" if report.get("active_run_id") else ""
    return (
        f"session={report['session_id']} turns={report['turns']} "
        f"completed={report['completed']} cancelled={report['cancelled']} "
        f"failed={report['failed']} errors={report['errors']} "
        f"sent={report['sent']} recv={report['received']} "
        f"last_run={report['last_run_id']}{active_part}{trace_part}{log_part}"
    )


async def run_interactive_operator(
    *,
    server: str,
    user_id: str,
    session_id: str | None,
    timeout: float,
    log_dir: str | None,
    quiet: bool,
) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional operator dependency.
        raise RuntimeError("Install websockets to use scripts/realtime_media_client.py") from exc

    sid = session_id or f"media-operator-{uuid.uuid4()}"
    state = OperatorSessionState(
        session_id=sid,
        log_path=_operator_log_path(log_dir, sid) if log_dir else None,
    )
    url = build_media_ws_url(server, user_id=user_id, session_id=sid, client="media_operator")
    stop = asyncio.Event()

    try:
        async with websockets.connect(url, open_timeout=min(10.0, timeout), close_timeout=2.0) as ws:
            await _operator_send(
                ws,
                state,
                session_start_event(user_id=user_id, session_id=sid),
                quiet=quiet,
            )
            ready = await _operator_recv(ws, state, timeout=timeout, quiet=quiet)
            if ready.get("type") != "call.ready":
                raise MediaSmokeError(f"expected call.ready, got {ready.get('type')!r}")
            if not quiet:
                print(f"operator ready session={sid}")
                if state.log_path:
                    print(f"operator log={state.log_path}")
                print(_operator_help())

            receiver = asyncio.create_task(
                _operator_receive_loop(ws, state=state, stop=stop, quiet=quiet)
            )
            try:
                await _operator_input_loop(
                    ws,
                    state=state,
                    server=server,
                    user_id=user_id,
                    session_id=sid,
                    stop=stop,
                    quiet=quiet,
                )
            finally:
                await _await_receiver_shutdown(receiver)
    except MediaSmokeError as exc:
        print(f"[failed] interactive: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[error] interactive: {exc}", file=sys.stderr)
        return 2

    _finish_assistant_line(state)
    print(json.dumps(state.report(), ensure_ascii=False))
    return 0


async def _run_one_scenario(
    *,
    ws: Any,
    name: str,
    user_id: str,
    session_id: str,
    text: str,
    timeout: float,
    strict_cancel: bool,
    quiet: bool,
) -> ScenarioResult:
    deadline = time.monotonic() + timeout
    await _send(ws, session_start_event(user_id=user_id, session_id=session_id), quiet=quiet)
    await _expect_type(ws, "call.ready", deadline=deadline, quiet=quiet)

    if name == "ping":
        await _send(ws, ping_event(user_id=user_id, session_id=session_id), quiet=quiet)
        await _expect_type(ws, "pong", deadline=deadline, quiet=quiet)
        await _send(ws, session_end_event(user_id=user_id, session_id=session_id), quiet=quiet)
        await _expect_type(ws, "call.hangup_ack", deadline=deadline, quiet=quiet)
        return ScenarioResult(name=name, session_id=session_id)

    await _send(ws, transcript_final_event(user_id=user_id, session_id=session_id, text=text), quiet=quiet)

    if name == "basic":
        result = await _collect_run_end(ws, deadline=deadline, quiet=quiet)
        if result.terminal_reason != "completed":
            raise MediaSmokeError(f"basic expected run.end reason=completed, got {result.terminal_reason!r}")
        return ScenarioResult(name=name, session_id=session_id, **result.__dict__)

    started = await _expect_type(ws, "run.started", deadline=deadline, quiet=quiet)
    run_id = _optional_string(started.get("run_id"))

    if name == "cancel":
        await _send(ws, run_cancel_event(user_id=user_id, session_id=session_id, run_id=run_id), quiet=quiet)
        result = await _collect_run_end(ws, deadline=deadline, quiet=quiet, initial_run_id=run_id)
        if strict_cancel and result.terminal_reason != "cancelled":
            raise MediaSmokeError(f"cancel expected reason=cancelled, got {result.terminal_reason!r}")
        return ScenarioResult(name=name, session_id=session_id, **result.__dict__)

    if name == "hangup":
        await _send(ws, session_end_event(user_id=user_id, session_id=session_id), quiet=quiet)
        ack = False
        terminal = ScenarioResult(name=name, session_id=session_id, run_id=run_id)
        while time.monotonic() < deadline:
            frame = await _recv(ws, deadline=deadline, quiet=quiet)
            frame_type = frame.get("type")
            if frame_type == "call.hangup_ack":
                ack = True
                if not strict_cancel:
                    return terminal
            elif frame_type == "run.end":
                terminal.run_id = _optional_string(frame.get("run_id")) or terminal.run_id
                terminal.terminal_reason = _optional_string(frame.get("reason"))
                if ack and (not strict_cancel or terminal.terminal_reason == "cancelled"):
                    return terminal
            elif frame_type == "stream.chunk":
                terminal.response_text += _chunk_text(frame)
            elif frame_type == "error":
                raise MediaSmokeError(_frame_error_message(frame))
        raise MediaSmokeError("hangup timed out waiting for call.hangup_ack")

    raise MediaSmokeError(f"unsupported scenario: {name}")


@dataclass
class _RunResult:
    run_id: str | None = None
    response_text: str = ""
    terminal_reason: str | None = None


async def _collect_run_end(
    ws: Any,
    *,
    deadline: float,
    quiet: bool,
    initial_run_id: str | None = None,
) -> _RunResult:
    result = _RunResult(run_id=initial_run_id)
    saw_started = bool(initial_run_id)
    while time.monotonic() < deadline:
        frame = await _recv(ws, deadline=deadline, quiet=quiet)
        frame_type = frame.get("type")
        if frame_type == "run.started":
            saw_started = True
            result.run_id = _optional_string(frame.get("run_id")) or result.run_id
        elif frame_type == "stream.chunk":
            result.response_text += _chunk_text(frame)
        elif frame_type == "run.end":
            result.run_id = _optional_string(frame.get("run_id")) or result.run_id
            result.terminal_reason = _optional_string(frame.get("reason"))
            if not saw_started:
                raise MediaSmokeError("received run.end before run.started")
            return result
        elif frame_type == "error":
            raise MediaSmokeError(_frame_error_message(frame))
    raise MediaSmokeError("timed out waiting for run.end")


async def _expect_type(ws: Any, expected: str, *, deadline: float, quiet: bool) -> dict[str, Any]:
    while time.monotonic() < deadline:
        frame = await _recv(ws, deadline=deadline, quiet=quiet)
        frame_type = frame.get("type")
        if frame_type == expected:
            return frame
        if frame_type == "error":
            raise MediaSmokeError(_frame_error_message(frame))
    raise MediaSmokeError(f"timed out waiting for {expected}")


async def _send(ws: Any, event: dict[str, Any], *, quiet: bool) -> None:
    if not quiet:
        print(json.dumps({"direction": "media -> gateway", "event": event}, ensure_ascii=False))
    await ws.send(json.dumps(event, ensure_ascii=False))


async def _recv(ws: Any, *, deadline: float, quiet: bool) -> dict[str, Any]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaSmokeError("timed out waiting for gateway frame")
    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
    try:
        frame = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MediaSmokeError(f"gateway returned invalid JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise MediaSmokeError("gateway returned a non-object frame")
    if not quiet:
        print(json.dumps({"direction": "gateway -> media", "frame": frame}, ensure_ascii=False))
    return frame


async def _operator_input_loop(
    ws: Any,
    *,
    state: OperatorSessionState,
    server: str,
    user_id: str,
    session_id: str,
    stop: asyncio.Event,
    quiet: bool,
) -> None:
    while not stop.is_set():
        try:
            line = await asyncio.to_thread(input, "call> ")
        except EOFError:
            command = OperatorCommand(kind="hangup", should_exit=True)
        else:
            command = parse_operator_command(line)
        await _handle_operator_command(
            ws,
            command,
            state=state,
            server=server,
            user_id=user_id,
            session_id=session_id,
            quiet=quiet,
        )
        if command.should_exit:
            return


async def _handle_operator_command(
    ws: Any,
    command: OperatorCommand,
    *,
    state: OperatorSessionState,
    server: str,
    user_id: str,
    session_id: str,
    quiet: bool,
) -> None:
    if command.kind == "noop":
        return
    if command.kind == "help":
        _finish_assistant_line(state)
        print(_operator_help())
        return
    if command.kind == "invalid":
        _finish_assistant_line(state)
        print(f"operator> {command.text}")
        print(_operator_help())
        return
    if command.kind == "report":
        _finish_assistant_line(state)
        print("operator> " + format_operator_report(state.report()))
        return
    if command.kind == "trace_last":
        await _run_trace_last(state, server=server)
        return
    if command.kind == "transcript":
        await _operator_send(
            ws,
            state,
            transcript_final_event(
                user_id=user_id,
                session_id=session_id,
                text=command.text,
                interrupt=command.interrupt,
            ),
            quiet=quiet,
        )
        return
    if command.kind == "cancel":
        await _operator_send(
            ws,
            state,
            run_cancel_event(
                user_id=user_id,
                session_id=session_id,
                run_id=state.active_run_id or state.last_run_id,
            ),
            quiet=quiet,
        )
        return
    if command.kind == "hangup":
        await _operator_send(
            ws,
            state,
            session_end_event(user_id=user_id, session_id=session_id, reason="operator_hangup"),
            quiet=quiet,
        )
        return
    if command.kind == "ping":
        await _operator_send(ws, state, ping_event(user_id=user_id, session_id=session_id), quiet=quiet)
        return
    raise MediaSmokeError(f"unsupported operator command kind: {command.kind}")


async def _operator_receive_loop(
    ws: Any,
    *,
    state: OperatorSessionState,
    stop: asyncio.Event,
    quiet: bool,
) -> None:
    saw_hangup_ack = False
    while not stop.is_set():
        try:
            frame = await _operator_recv(ws, state, timeout=None, quiet=quiet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not stop.is_set():
                print(f"operator> receive loop ended: {exc}", file=sys.stderr)
            return
        frame_type = frame.get("type")
        if frame_type == "call.hangup_ack":
            saw_hangup_ack = True
        if saw_hangup_ack and state.active_run_id is None:
            stop.set()
            return


async def _operator_send(
    ws: Any,
    state: OperatorSessionState,
    event: dict[str, Any],
    *,
    quiet: bool,
) -> None:
    state.record_send(event)
    if not quiet:
        _finish_assistant_line(state)
        print(_format_operator_send(event))
    await ws.send(json.dumps(event, ensure_ascii=False))


async def _operator_recv(
    ws: Any,
    state: OperatorSessionState,
    *,
    timeout: float | None,
    quiet: bool,
) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout) if timeout is not None else await ws.recv()
    frame = _json_object(raw, source="gateway frame")
    state.record_recv(frame)
    if not quiet:
        _print_operator_recv(frame, state)
    return frame


async def _await_receiver_shutdown(receiver: asyncio.Task[None]) -> None:
    if receiver.done():
        await receiver
        return
    try:
        await asyncio.wait_for(receiver, timeout=2.0)
    except asyncio.TimeoutError:
        receiver.cancel()
        try:
            await receiver
        except asyncio.CancelledError:
            return


async def _run_trace_last(state: OperatorSessionState, *, server: str) -> None:
    _finish_assistant_line(state)
    if not state.last_trace_id:
        print("operator> no trace_id captured yet")
        return
    command = [sys.executable, str(TRACE_VIEW_SCRIPT), state.last_trace_id, "--server", server]
    print("operator> " + format_trace_view_command(state.last_trace_id, server=server))
    await asyncio.to_thread(subprocess.run, command, cwd=str(REPO_ROOT), check=False)


def _operator_help() -> str:
    return "\n".join(
        [
            "commands: plain text sends transcript.final",
            "/interrupt <text>  send interrupt transcript",
            "/cancel            cancel active run",
            "/hangup            end session",
            "/report            print session summary",
            "/trace last        inspect last trace with trace_view.py",
            "/ping              send ping",
            "/quit              end session",
        ]
    )


def _operator_log_path(log_dir: str, session_id: str) -> Path:
    safe_session_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)
    return Path(log_dir) / f"{safe_session_id}.jsonl"


def _json_object(raw: str, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MediaSmokeError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise MediaSmokeError(f"{source} returned a non-object")
    return value


def _format_operator_send(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    if event_type == "transcript.final":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        text = str(payload.get("text") or "")
        marker = " interrupt" if payload.get("interrupt") is True else ""
        return f"media>{marker} {text}"
    return f"media> {event_type}"


def _format_operator_recv(frame: dict[str, Any]) -> str:
    frame_type = str(frame.get("type") or "")
    if frame_type == "stream.chunk":
        return f"assistant> {_chunk_text(frame)}"
    if frame_type == "event.progress":
        return f"progress> {_chunk_text(frame)}"
    if frame_type == "event.tool":
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        tool_name = payload.get("tool_name") or payload.get("name") or ""
        status = payload.get("status") or payload.get("event") or ""
        return f"tool> {tool_name} {status}".rstrip()
    if frame_type == "run.started":
        return f"run> started {frame.get('run_id')}"
    if frame_type == "run.end":
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        trace_id = payload.get("trace_id")
        trace_part = f" trace={trace_id}" if trace_id else ""
        return f"run> end {frame.get('reason')}{trace_part}"
    if frame_type == "error":
        return f"error> {_frame_error_message(frame)}"
    return f"gateway> {json.dumps(frame, ensure_ascii=False)}"


def _print_operator_recv(frame: dict[str, Any], state: OperatorSessionState) -> None:
    frame_type = str(frame.get("type") or "")
    if frame_type == "stream.chunk":
        text = _chunk_text(frame)
        if not text:
            return
        if not state.assistant_line_open:
            print("assistant> ", end="", flush=True)
            state.assistant_line_open = True
        print(text, end="", flush=True)
        return
    _finish_assistant_line(state)
    print(_format_operator_recv(frame))


def _finish_assistant_line(state: OperatorSessionState) -> None:
    if state.assistant_line_open:
        print("", flush=True)
        state.assistant_line_open = False


def _selected_scenarios(scenario: str) -> tuple[str, ...]:
    if scenario == "all":
        return ("ping", "basic", "cancel", "hangup")
    if scenario not in SCENARIOS:
        raise MediaSmokeError(f"unknown scenario: {scenario}")
    return (scenario,)


def _chunk_text(frame: dict[str, Any]) -> str:
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    return "" if text is None else str(text)


def _frame_error_message(frame: dict[str, Any]) -> str:
    error = frame.get("error")
    if isinstance(error, dict):
        code = error.get("code") or "error"
        message = error.get("message") or ""
        return f"{code}: {message}"
    return "gateway returned error frame"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.interactive:
        return asyncio.run(
            run_interactive_operator(
                server=args.server,
                user_id=args.user_id,
                session_id=args.session_id,
                timeout=args.timeout,
                log_dir=args.log_dir,
                quiet=args.quiet,
            )
        )
    return asyncio.run(
        run_media_smoke(
            server=args.server,
            scenario=args.scenario,
            user_id=args.user_id,
            session_id=args.session_id,
            text=args.text,
            timeout=args.timeout,
            strict_cancel=args.strict_cancel,
            quiet=args.quiet,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
