#!/usr/bin/env python3
"""In-process text-only realtime call simulator for `/ws/realtime/media`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.api import gateway_runtime
from assistant_agent.api.app import create_app
from assistant_agent.gateway import GatewayBridge, GatewaySessionManager
from assistant_agent.realtime import RealtimeAgentEvent, RealtimeAgentResult

SCENARIOS = ("basic", "interrupt", "hangup", "cancel", "tool_interrupt", "all")
DEFAULT_TEXT = "你好，这是一次文本实时通话测试。"
SLOW_TEXT = "__text_realtime_simulator_slow_turn__"
TOOL_TEXT = "__text_realtime_simulator_tool_turn__"


class SimulatorError(RuntimeError):
    """Raised when a simulator scenario observes an invalid realtime flow."""


@dataclass
class ScenarioSummary:
    scenario: str
    status: str
    session_id: str
    frames: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)
    final_texts: list[str] = field(default_factory=list)
    terminal_reasons: list[str] = field(default_factory=list)
    hangup_cancelled_active_run: bool | None = None
    latency_ms: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "session_id": self.session_id,
            "frames": self.frames,
            "run_ids": self.run_ids,
            "trace_ids": self.trace_ids,
            "final_texts": self.final_texts,
            "terminal_reasons": self.terminal_reasons,
            "hangup_cancelled_active_run": self.hangup_cancelled_active_run,
            "latency_ms": self.latency_ms,
            "requests": self.requests,
        }


class TextSimulatorBackend:
    """Deterministic text backend for Gateway lifecycle simulation."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def run_turn(self, request, *, event_sink=None, cancel_token=None):
        self.requests.append(request)
        index = len(self.requests)
        trace_id = f"trace-text-realtime-sim-{index}"
        if request.text == SLOW_TEXT:
            await _wait_for_cancel(cancel_token)
            return RealtimeAgentResult(
                status="cancelled",
                run_id=request.run_id,
                trace_id=trace_id,
                expects_reply=True,
            )

        if request.text == TOOL_TEXT:
            if event_sink is not None:
                await event_sink(
                    RealtimeAgentEvent(
                        type="tool.started",
                        text="simulator slow tool started",
                        payload={"tool_name": "simulator_slow_tool"},
                    )
                )
            await _wait_for_cancel(cancel_token)
            if event_sink is not None:
                await event_sink(
                    RealtimeAgentEvent(
                        type="tool.finished",
                        text="stale tool result should not leak",
                        payload={"tool_name": "simulator_slow_tool"},
                    )
                )
                await event_sink(
                    RealtimeAgentEvent(
                        type="response.chunk",
                        text="stale tool result should not leak",
                        payload={"simulator": "text_realtime"},
                    )
                )
            return RealtimeAgentResult(
                status="completed",
                response_text="stale tool result should not leak",
                run_id=request.run_id,
                trace_id=trace_id,
                expects_reply=True,
            )

        response_text = f"simulated text reply: {request.text}"
        if event_sink is not None:
            await event_sink(
                RealtimeAgentEvent(
                    type="response.chunk",
                    text=response_text,
                    payload={"simulator": "text_realtime"},
                )
            )
        return RealtimeAgentResult(
            status="completed",
            response_text=response_text,
            run_id=request.run_id,
            trace_id=trace_id,
            expects_reply=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run text-only realtime Gateway simulations in process."
    )
    parser.add_argument("--scenario", choices=SCENARIOS, default="basic")
    parser.add_argument("--user-id", default="text_realtime_sim_user")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--quiet", action="store_true", help="Only print scenario summaries.")
    return parser


def run_simulator(
    *,
    scenario: str,
    user_id: str,
    session_id: str | None,
    text: str,
    quiet: bool,
) -> list[ScenarioSummary]:
    selected = _selected_scenarios(scenario)
    summaries: list[ScenarioSummary] = []
    for name in selected:
        sid = session_id if len(selected) == 1 and session_id else f"text-sim-{name}-{uuid.uuid4()}"
        summaries.append(
            _run_one_scenario(
                name=name,
                user_id=user_id,
                session_id=sid,
                text=text,
                quiet=quiet,
            )
        )
    return summaries


def _run_one_scenario(
    *,
    name: str,
    user_id: str,
    session_id: str,
    text: str,
    quiet: bool,
) -> ScenarioSummary:
    backend = TextSimulatorBackend()
    manager = GatewaySessionManager(backend_factory=lambda: backend, start_reaper=False)
    gateway_runtime.set_gateway_runtime_for_tests(
        manager=manager,
        bridge=GatewayBridge(session_manager=manager),
    )
    started_at = time.perf_counter()
    summary = ScenarioSummary(scenario=name, status="failed", session_id=session_id)
    try:
        with TestClient(create_app()) as client:
            with client.websocket_connect(_media_ws_path(user_id=user_id, session_id=session_id)) as ws:
                _send(ws, _session_start_event(user_id=user_id, session_id=session_id), quiet=quiet)
                _record(summary, _recv(ws, quiet=quiet))

                if name == "basic":
                    _run_basic(ws, summary, user_id=user_id, session_id=session_id, text=text, quiet=quiet)
                elif name == "interrupt":
                    _run_interrupt(
                        ws,
                        summary,
                        user_id=user_id,
                        session_id=session_id,
                        text=text,
                        quiet=quiet,
                    )
                elif name == "hangup":
                    _run_hangup(ws, summary, user_id=user_id, session_id=session_id, quiet=quiet)
                elif name == "cancel":
                    _run_cancel(ws, summary, user_id=user_id, session_id=session_id, quiet=quiet)
                elif name == "tool_interrupt":
                    _run_tool_interrupt(
                        ws,
                        summary,
                        user_id=user_id,
                        session_id=session_id,
                        text=text,
                        quiet=quiet,
                    )
                else:
                    raise SimulatorError(f"unsupported scenario: {name}")

        summary.status = "passed"
        summary.requests = [_request_summary(request) for request in backend.requests]
        summary.latency_ms = int((time.perf_counter() - started_at) * 1000)
        _validate_summary(summary)
        return summary
    finally:
        gateway_runtime.reset_gateway_runtime_for_tests()


def _run_basic(ws, summary: ScenarioSummary, *, user_id: str, session_id: str, text: str, quiet: bool) -> None:
    _send(ws, _transcript_final_event(user_id=user_id, session_id=session_id, text=text), quiet=quiet)
    _receive_until(summary, ws, {"run.end"}, quiet=quiet)
    _send(ws, _session_end_event(user_id=user_id, session_id=session_id), quiet=quiet)
    _receive_until(summary, ws, {"call.hangup_ack"}, quiet=quiet)


def _run_interrupt(
    ws,
    summary: ScenarioSummary,
    *,
    user_id: str,
    session_id: str,
    text: str,
    quiet: bool,
) -> None:
    _send(ws, _transcript_final_event(user_id=user_id, session_id=session_id, text=SLOW_TEXT), quiet=quiet)
    _receive_until(summary, ws, {"run.started"}, quiet=quiet)
    _send(
        ws,
        _transcript_final_event(
            user_id=user_id,
            session_id=session_id,
            text=text,
            interrupt=True,
        ),
        quiet=quiet,
    )
    _receive_until_run_reasons(summary, ws, {"cancelled", "completed"}, quiet=quiet)
    _send(ws, _session_end_event(user_id=user_id, session_id=session_id), quiet=quiet)
    _receive_until(summary, ws, {"call.hangup_ack"}, quiet=quiet)


def _run_hangup(ws, summary: ScenarioSummary, *, user_id: str, session_id: str, quiet: bool) -> None:
    _send(ws, _transcript_final_event(user_id=user_id, session_id=session_id, text=SLOW_TEXT), quiet=quiet)
    _receive_until(summary, ws, {"run.started"}, quiet=quiet)
    _send(
        ws,
        _session_end_event(user_id=user_id, session_id=session_id, reason="simulator_hangup"),
        quiet=quiet,
    )
    _receive_until(summary, ws, {"call.hangup_ack", "run.end"}, quiet=quiet)


def _run_cancel(ws, summary: ScenarioSummary, *, user_id: str, session_id: str, quiet: bool) -> None:
    _send(ws, _transcript_final_event(user_id=user_id, session_id=session_id, text=SLOW_TEXT), quiet=quiet)
    _receive_until(summary, ws, {"run.started"}, quiet=quiet)
    _send(
        ws,
        _run_cancel_event(
            user_id=user_id,
            session_id=session_id,
            run_id=summary.run_ids[-1] if summary.run_ids else None,
            reason="simulator_cancel",
        ),
        quiet=quiet,
    )
    _receive_until(summary, ws, {"run.end"}, quiet=quiet)
    _send(ws, _session_end_event(user_id=user_id, session_id=session_id), quiet=quiet)
    _receive_until(summary, ws, {"call.hangup_ack"}, quiet=quiet)


def _run_tool_interrupt(
    ws,
    summary: ScenarioSummary,
    *,
    user_id: str,
    session_id: str,
    text: str,
    quiet: bool,
) -> None:
    _send(ws, _transcript_final_event(user_id=user_id, session_id=session_id, text=TOOL_TEXT), quiet=quiet)
    _receive_until(summary, ws, {"run.started"}, quiet=quiet)
    _send(
        ws,
        _transcript_final_event(
            user_id=user_id,
            session_id=session_id,
            text=text,
            interrupt=True,
        ),
        quiet=quiet,
    )
    _receive_until_run_reasons(summary, ws, {"cancelled", "completed"}, quiet=quiet)
    _send(ws, _session_end_event(user_id=user_id, session_id=session_id), quiet=quiet)
    _receive_until(summary, ws, {"call.hangup_ack"}, quiet=quiet)


def _receive_until(
    summary: ScenarioSummary,
    ws,
    expected_types: set[str],
    *,
    quiet: bool,
    limit: int = 20,
) -> None:
    remaining = set(expected_types)
    for _ in range(limit):
        frame = _recv(ws, quiet=quiet)
        _record(summary, frame)
        remaining.discard(str(frame.get("type") or ""))
        if not remaining:
            return
    raise SimulatorError(f"timed out waiting for {sorted(remaining)}")


def _receive_until_run_reasons(
    summary: ScenarioSummary,
    ws,
    expected_reasons: set[str],
    *,
    quiet: bool,
    limit: int = 30,
) -> None:
    remaining = set(expected_reasons)
    for _ in range(limit):
        frame = _recv(ws, quiet=quiet)
        _record(summary, frame)
        if frame.get("type") == "run.end":
            remaining.discard(str(frame.get("reason") or ""))
        if not remaining:
            return
    raise SimulatorError(f"timed out waiting for run.end reasons {sorted(remaining)}")


def _record(summary: ScenarioSummary, frame: dict[str, Any]) -> None:
    frame_type = str(frame.get("type") or "")
    summary.frames.append(frame_type)
    run_id = _optional_string(frame.get("run_id"))
    if run_id and run_id not in summary.run_ids:
        summary.run_ids.append(run_id)
    payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
    trace_id = _optional_string(payload.get("trace_id"))
    if trace_id and trace_id not in summary.trace_ids:
        summary.trace_ids.append(trace_id)
    if frame_type == "stream.chunk":
        text = _optional_string(payload.get("text"))
        if text:
            summary.final_texts.append(text)
    if frame_type == "run.end":
        reason = _optional_string(frame.get("reason"))
        if reason:
            summary.terminal_reasons.append(reason)
    if frame_type == "call.hangup_ack":
        cancelled = payload.get("cancelled_active_run")
        if isinstance(cancelled, bool):
            summary.hangup_cancelled_active_run = cancelled
    if frame_type == "error":
        raise SimulatorError(_frame_error_message(frame))


def _validate_summary(summary: ScenarioSummary) -> None:
    if summary.scenario == "basic":
        if summary.terminal_reasons != ["completed"]:
            raise SimulatorError(f"basic expected one completed run, got {summary.terminal_reasons}")
        if summary.hangup_cancelled_active_run is not False:
            raise SimulatorError("basic hangup should not cancel an already completed run")
    elif summary.scenario == "interrupt":
        if set(summary.terminal_reasons) != {"cancelled", "completed"}:
            raise SimulatorError(f"interrupt expected cancelled+completed, got {summary.terminal_reasons}")
        if summary.hangup_cancelled_active_run is not False:
            raise SimulatorError("interrupt cleanup hangup should not cancel a completed run")
    elif summary.scenario == "hangup":
        if "cancelled" not in summary.terminal_reasons:
            raise SimulatorError(f"hangup expected a cancelled run, got {summary.terminal_reasons}")
        if summary.hangup_cancelled_active_run is not True:
            raise SimulatorError("hangup should cancel the active run")
    elif summary.scenario == "cancel":
        if summary.terminal_reasons != ["cancelled"]:
            raise SimulatorError(f"cancel expected one cancelled run, got {summary.terminal_reasons}")
        if summary.hangup_cancelled_active_run is not False:
            raise SimulatorError("cancel cleanup hangup should not cancel a completed run")
    elif summary.scenario == "tool_interrupt":
        if set(summary.terminal_reasons) != {"cancelled", "completed"}:
            raise SimulatorError(
                f"tool_interrupt expected cancelled+completed, got {summary.terminal_reasons}"
            )
        if any("stale tool result" in text for text in summary.final_texts):
            raise SimulatorError("tool_interrupt leaked stale tool output")
        if summary.hangup_cancelled_active_run is not False:
            raise SimulatorError("tool_interrupt cleanup hangup should not cancel a completed run")
    if not summary.requests:
        raise SimulatorError("scenario did not reach the realtime backend")
    for request in summary.requests:
        if request["audio_id"] is not None or request["image_ids"] or request["video_ids"]:
            raise SimulatorError(f"simulator is text-only, got media refs: {request}")
        if request["source"] != "realtime_media_websocket":
            raise SimulatorError(f"unexpected request source: {request['source']!r}")


def _session_start_event(*, user_id: str, session_id: str) -> dict[str, Any]:
    return {
        "type": "session.start",
        "session_id": session_id,
        "user_id": user_id,
        "payload": {
            "session_id": session_id,
            "config": {
                "entry": "text_realtime_simulator",
                "identity_bound": True,
                "locale": "zh-CN",
                "mode": "text",
            },
        },
    }


def _transcript_final_event(
    *,
    user_id: str,
    session_id: str,
    text: str,
    interrupt: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text,
        "metadata": {"source": "text_realtime_simulator"},
    }
    if interrupt:
        payload["interrupt"] = True
    return {
        "type": "transcript.final",
        "session_id": session_id,
        "user_id": user_id,
        "payload": payload,
    }


def _session_end_event(
    *,
    user_id: str,
    session_id: str,
    reason: str = "simulator_session_end",
) -> dict[str, Any]:
    return {
        "type": "session.end",
        "session_id": session_id,
        "user_id": user_id,
        "payload": {"reason": reason},
    }


def _run_cancel_event(
    *,
    user_id: str,
    session_id: str,
    run_id: str | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "type": "run.cancel",
        "session_id": session_id,
        "user_id": user_id,
        "run_id": run_id,
        "payload": {"reason": reason},
    }


def _request_summary(request: Any) -> dict[str, Any]:
    return {
        "text": request.text,
        "audio_id": request.audio_id,
        "image_ids": list(request.image_ids),
        "video_ids": list(request.video_ids),
        "source": request.metadata.get("source"),
        "source_detail": request.metadata.get("source_detail"),
        "session_config": request.metadata.get("gateway", {}).get("session_config"),
    }


def _media_ws_path(*, user_id: str, session_id: str) -> str:
    query = urlencode({"user_id": user_id, "session_id": session_id, "client": "media_service"})
    return f"/ws/realtime/media?{query}"


def _send(ws, event: dict[str, Any], *, quiet: bool) -> None:
    if not quiet:
        print(json.dumps({"direction": "media -> gateway", "event": event}, ensure_ascii=False))
    ws.send_json(event)


def _recv(ws, *, quiet: bool) -> dict[str, Any]:
    frame = ws.receive_json()
    if not isinstance(frame, dict):
        raise SimulatorError("gateway returned a non-object frame")
    if not quiet:
        print(json.dumps({"direction": "gateway -> media", "frame": frame}, ensure_ascii=False))
    return frame


async def _wait_for_cancel(cancel_token: Any) -> None:
    if cancel_token is None:
        await asyncio.sleep(0)
        return
    while not cancel_token.is_cancelled():
        await asyncio.sleep(0.01)


def _selected_scenarios(scenario: str) -> tuple[str, ...]:
    if scenario == "all":
        return ("basic", "interrupt", "hangup", "cancel", "tool_interrupt")
    if scenario not in SCENARIOS:
        raise SimulatorError(f"unsupported scenario: {scenario}")
    return (scenario,)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _frame_error_message(frame: dict[str, Any]) -> str:
    error = frame.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        return str(message or error)
    return str(error or frame)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summaries = run_simulator(
            scenario=args.scenario,
            user_id=args.user_id,
            session_id=args.session_id,
            text=args.text,
            quiet=args.quiet,
        )
    except SimulatorError as exc:
        print(f"[failed] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - script boundary.
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    for summary in summaries:
        print(json.dumps(summary.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
