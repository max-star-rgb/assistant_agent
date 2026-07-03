#!/usr/bin/env python3
"""Media Relay protocol smoke client for `/ws/realtime/media`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode


SCENARIOS = ("basic", "ping", "cancel", "hangup", "all")
DEFAULT_TEXT = "Reply exactly REAL_LLM_OK and do not call tools."


class MediaSmokeError(RuntimeError):
    """Raised when the media protocol smoke detects an invalid frame sequence."""


@dataclass
class ScenarioResult:
    name: str
    session_id: str
    run_id: str | None = None
    response_text: str = ""
    terminal_reason: str | None = None


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


def transcript_final_event(*, user_id: str, session_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "transcript.final",
        "session_id": session_id,
        "user_id": user_id,
        "payload": {
            "text": text,
            "metadata": {
                "source": "scripted_media_relay",
                "transport": "websocket",
            },
        },
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
