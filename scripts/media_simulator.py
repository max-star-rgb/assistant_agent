"""Media-Agent protocol console client for /agent-service/v1."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.identifiers import new_prefixed_uuid7  # noqa: E402
from assistant_agent.runtime.generated_artifacts import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    MAX_DELIVERED_IMAGE_COUNT,
)


AGENT_SERVICE_MAX_MESSAGE_BYTES = (
    ((MAX_ARTIFACT_BYTES + 2) // 3) * 4 * MAX_DELIVERED_IMAGE_COUNT
    + 1024 * 1024
)

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class MediaChatOutcome:
    ok: bool
    workflow_ids: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)


async def run_media_console(
    *,
    server: str,
    user_number: str,
    session_id: str | None,
    initial_text: str | None,
    call_type: str = "AUDIO",
    model_name: str | None = None,
    stream: bool = False,
    chat_progress: bool = False,
    chat_response_ack: bool = False,
    citations: bool = False,
    citation_debug: bool = False,
    interactive: bool = False,
    workflow_details: bool = False,
    wait_proactive: bool = False,
) -> int:
    """Run a Media-Agent compatible client.

    When ``initial_text`` is provided and ``interactive`` is false, this sends one
    chat message and exits. Otherwise it stays in a console loop.
    """

    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional operator dependency.
        raise RuntimeError("Install websockets to use scripts/media_simulator.py") from exc

    current_session_id = session_id or new_session_id()
    websocket = await _open_media_session(
        websockets,
        server=server,
        session_id=current_session_id,
        user_number=user_number,
        call_type=call_type,
        model_name=model_name,
        chat_progress=chat_progress,
        chat_response_ack=chat_response_ack,
        citations=citations,
    )
    chat_counter = 0
    assistant_mode = "standard"
    try:
        if initial_text:
            chat_counter += 1
            try:
                outcome = await _send_chat_and_print_responses(
                    websocket,
                    text=initial_text,
                    chat_index=f"chat-{chat_counter}",
                    user_number=user_number,
                    session_id=current_session_id,
                    stream=stream,
                    chat_response_ack=chat_response_ack,
                    citation_debug=citation_debug,
                    assistant_mode=assistant_mode,
                )
            except websockets.exceptions.ConnectionClosed as exc:
                _print_interrupted_turn(exc)
                if not interactive:
                    return 1
                try:
                    websocket = await _reopen_media_session(
                        websockets,
                        websocket,
                        server=server,
                        session_id=current_session_id,
                        user_number=user_number,
                        call_type=call_type,
                        model_name=model_name,
                        chat_progress=chat_progress,
                        chat_response_ack=chat_response_ack,
                        citations=citations,
                    )
                except Exception as reconnect_exc:
                    _print_reconnect_error(reconnect_exc)
                    return 1
                print(
                    f"Reconnected session {current_session_id}; "
                    f"assistant mode remains {assistant_mode}.",
                    flush=True,
                )
                outcome = MediaChatOutcome(ok=False)
            ok = outcome.ok
            if ok:
                ok = await _tail_submitted_workflows(
                    outcome.workflow_ids,
                    server=server,
                    user_number=user_number,
                    session_id=current_session_id,
                    interactive=interactive,
                    workflow_details=workflow_details,
                )
            if ok and wait_proactive:
                if not outcome.task_ids:
                    print(
                        "ERROR: Agent response did not create a durable task.",
                        file=sys.stderr,
                        flush=True,
                    )
                    ok = False
                else:
                    websocket, ok = await _wait_for_durable_notification(
                        websockets,
                        websocket,
                        task_ids=outcome.task_ids,
                        server=server,
                        session_id=current_session_id,
                        user_number=user_number,
                        call_type=call_type,
                        model_name=model_name,
                        chat_progress=chat_progress,
                        chat_response_ack=chat_response_ack,
                        citations=citations,
                    )
            if not interactive:
                return 0 if ok else 1

        _print_console_help(current_session_id, assistant_mode=assistant_mode)
        while True:
            try:
                line = await asyncio.to_thread(input, f"[{current_session_id}]> ")
            except EOFError:
                print()
                return 0
            command, value = parse_console_command(line)
            if command == "empty":
                continue
            if command == "quit":
                return 0
            if command == "help":
                _print_console_help(current_session_id, assistant_mode=assistant_mode)
                continue
            if command == "unknown":
                print(f"Unknown command: {value}. Type /help for commands.", flush=True)
                continue
            if command == "new":
                await websocket.close()
                current_session_id = value or new_session_id()
                websocket = await _open_media_session(
                    websockets,
                    server=server,
                    session_id=current_session_id,
                    user_number=user_number,
                    call_type=call_type,
                    model_name=model_name,
                    chat_progress=chat_progress,
                    chat_response_ack=chat_response_ack,
                    citations=citations,
                )
                chat_counter = 0
                print(f"Opened session {current_session_id}.", flush=True)
                continue
            if command == "mode":
                assistant_mode = str(value)
                print(f"Assistant mode: {assistant_mode}.", flush=True)
                continue

            chat_counter += 1
            try:
                outcome = await _send_chat_and_print_responses(
                    websocket,
                    text=str(value),
                    chat_index=f"chat-{chat_counter}",
                    user_number=user_number,
                    session_id=current_session_id,
                    stream=stream,
                    chat_response_ack=chat_response_ack,
                    citation_debug=citation_debug,
                    assistant_mode=assistant_mode,
                )
            except websockets.exceptions.ConnectionClosed as exc:
                _print_interrupted_turn(exc)
                try:
                    websocket = await _reopen_media_session(
                        websockets,
                        websocket,
                        server=server,
                        session_id=current_session_id,
                        user_number=user_number,
                        call_type=call_type,
                        model_name=model_name,
                        chat_progress=chat_progress,
                        chat_response_ack=chat_response_ack,
                        citations=citations,
                    )
                except Exception as reconnect_exc:
                    _print_reconnect_error(reconnect_exc)
                    return 1
                print(
                    f"Reconnected session {current_session_id}; "
                    f"assistant mode remains {assistant_mode}.",
                    flush=True,
                )
                continue
            if outcome.ok:
                await _tail_submitted_workflows(
                    outcome.workflow_ids,
                    server=server,
                    user_number=user_number,
                    session_id=current_session_id,
                    interactive=True,
                    workflow_details=workflow_details,
                )
                if wait_proactive and outcome.task_ids:
                    websocket, _ = await _wait_for_durable_notification(
                        websockets,
                        websocket,
                        task_ids=outcome.task_ids,
                        server=server,
                        session_id=current_session_id,
                        user_number=user_number,
                        call_type=call_type,
                        model_name=model_name,
                        chat_progress=chat_progress,
                        chat_response_ack=chat_response_ack,
                        citations=citations,
                    )
    finally:
        await websocket.close()


async def _open_media_session(
    websockets_module: Any,
    *,
    server: str,
    session_id: str | None,
    user_number: str,
    call_type: str,
    model_name: str | None,
    chat_progress: bool,
    chat_response_ack: bool,
    citations: bool = False,
) -> Any:
    url = agent_service_ws_url(server, session_id=session_id)
    websocket = await websockets_module.connect(
        url,
        max_size=AGENT_SERVICE_MAX_MESSAGE_BYTES,
    )
    await websocket.send(
        json.dumps(
            media_envelope(
                "assistantControl",
                assistant_control_body(
                    user_number=user_number,
                    call_type=call_type,
                    model_name=model_name,
                    chat_progress=chat_progress,
                    chat_response_ack=chat_response_ack,
                    citations=citations,
                ),
                session_id=session_id,
            ),
            ensure_ascii=False,
        )
    )
    await websocket.recv()
    return websocket


async def _reopen_media_session(
    websockets_module: Any,
    websocket: Any,
    **open_kwargs: Any,
) -> Any:
    try:
        await websocket.close()
    except Exception:
        pass
    return await _open_media_session(websockets_module, **open_kwargs)


async def _send_chat_and_print_responses(
    websocket: Any,
    *,
    text: str,
    chat_index: str,
    user_number: str,
    session_id: str | None,
    stream: bool,
    chat_response_ack: bool,
    citation_debug: bool = False,
    assistant_mode: str = "standard",
) -> MediaChatOutcome:
    await websocket.send(
        json.dumps(
            media_envelope(
                "chat",
                chat_body(
                    text=text,
                    chat_index=chat_index,
                    user_number=user_number,
                    speaker_number=user_number,
                    stream=stream,
                    assistant_mode=assistant_mode,
                ),
                session_id=session_id,
            ),
            ensure_ascii=False,
        )
    )
    printed_response_text = False
    while True:
        envelope = json.loads(await websocket.recv())
        message = str(envelope.get("message") or "")
        body = parse_body(envelope)
        if message == "error":
            _print_protocol_error(body)
            return MediaChatOutcome(ok=False)
        if message != "chatResponse":
            continue
        if chat_response_error(body) is not None:
            _print_protocol_error(body)
            return MediaChatOutcome(ok=False)
        response_chat_index = _chat_index_from_chat_response_body(body)
        if response_chat_index is not None and response_chat_index != chat_index:
            continue
        description = chat_response_description(body)
        final = body.get("final")
        if final is False:
            if description:
                print(description, end="", flush=True)
                printed_response_text = True
            continue
        if chat_response_ack and body.get("deliveryId"):
            await _send_chat_response_ack(
                websocket,
                delivery_id=str(body["deliveryId"]),
                chat_index=chat_index,
                session_id=session_id,
            )
        if description and printed_response_text:
            print(description, end="", flush=True)
            print(flush=True)
        elif description:
            print(description, flush=True)
        elif printed_response_text:
            print(flush=True)
        if citation_debug:
            _print_citation_sources(body)
        status = _intent_status(body)
        return MediaChatOutcome(
            ok=status != "FAIL",
            workflow_ids=_workflow_ids_from_chat_response_body(body),
            task_ids=_task_ids_from_chat_response_body(body),
        )


async def _wait_for_durable_notification(
    websockets_module: Any,
    websocket: Any,
    *,
    task_ids: list[str],
    server: str,
    session_id: str,
    user_number: str,
    call_type: str,
    model_name: str | None,
    chat_progress: bool,
    chat_response_ack: bool,
    citations: bool,
) -> tuple[Any, bool]:
    expected_indexes = {f"durable-task:{task_id}" for task_id in task_ids}
    print("Waiting for a proactive durable-task reminder…", flush=True)
    while True:
        try:
            envelope = json.loads(await websocket.recv())
        except websockets_module.exceptions.ConnectionClosed:
            print(
                "Agent-Service connection closed while waiting; reconnecting.",
                file=sys.stderr,
                flush=True,
            )
            reconnect_delay_s = 1.0
            while True:
                try:
                    websocket = await _reopen_media_session(
                        websockets_module,
                        websocket,
                        server=server,
                        session_id=session_id,
                        user_number=user_number,
                        call_type=call_type,
                        model_name=model_name,
                        chat_progress=chat_progress,
                        chat_response_ack=chat_response_ack,
                        citations=citations,
                    )
                    print(
                        f"Reconnected session {session_id}; still waiting for reminder.",
                        flush=True,
                    )
                    break
                except Exception as reconnect_exc:
                    _print_reconnect_error(reconnect_exc)
                    await asyncio.sleep(reconnect_delay_s)
                    reconnect_delay_s = min(5.0, reconnect_delay_s * 2)
            continue
        if not isinstance(envelope, dict):
            continue
        body = parse_body(envelope)
        if envelope.get("message") == "error":
            _print_protocol_error(body)
            return websocket, False
        if envelope.get("message") != "chatResponse":
            continue
        chat_index = _chat_index_from_chat_response_body(body)
        if chat_index not in expected_indexes:
            continue
        description = chat_response_description(body)
        if description:
            print(description, flush=True)
        return websocket, _intent_status(body) != "FAIL"


async def _tail_submitted_workflows(
    workflow_ids: list[str],
    *,
    server: str,
    user_number: str,
    session_id: str,
    interactive: bool = False,
    workflow_details: bool = False,
) -> bool:
    outcomes = []
    for workflow_id in workflow_ids:
        outcomes.append(await tail_workflow(
            server=server,
            workflow_id=workflow_id,
            user_number=user_number,
            session_id=session_id,
            interactive=interactive,
            workflow_details=workflow_details,
        ))
    return all(outcomes) if outcomes else True


async def tail_workflow(
    *,
    server: str,
    workflow_id: str,
    user_number: str,
    session_id: str,
    poll_seconds: float = 1.0,
    interactive: bool = False,
    workflow_details: bool = False,
) -> bool:
    cursor = 0
    last_error = ""
    last_progress_key = ""
    if workflow_details:
        print(f"Tailing workflow {workflow_id}...", flush=True)
    else:
        print("正在跟踪研究进度…", flush=True)
    while True:
        try:
            events_payload = await asyncio.to_thread(
                _workflow_api_get,
                server,
                f"/workflows/{workflow_id}/events",
                user_number,
                session_id,
                {"after": cursor, "limit": 100},
            )
            events = events_payload.get("events")
            next_cursor = events_payload.get("next_cursor")
            if isinstance(next_cursor, int) and next_cursor >= cursor:
                cursor = next_cursor

            status_payload = await asyncio.to_thread(
                _workflow_api_get,
                server,
                f"/workflows/{workflow_id}",
                user_number,
                session_id,
                {},
            )
            last_error = ""
        except Exception as exc:
            message = str(exc)
            if message != last_error:
                print(
                    f"WARNING: Workflow tail unavailable: {message}; retrying.",
                    file=sys.stderr,
                    flush=True,
                )
                last_error = message
            await asyncio.sleep(max(poll_seconds, 0.1))
            continue

        workflow = status_payload.get("workflow")
        status = str(workflow.get("status") or "") if isinstance(workflow, dict) else ""
        if workflow_details and isinstance(events, list):
            for event in events:
                if isinstance(event, dict):
                    _print_workflow_event(workflow_id, event)
        progress = project_workflow_progress(status_payload)
        progress_key = json.dumps(progress, ensure_ascii=False, sort_keys=True)
        if progress and progress_key != last_progress_key:
            print(_workflow_progress_message(progress), flush=True)
            last_progress_key = progress_key
        if status == "completed":
            result_text = ""
            try:
                result_payload = await asyncio.to_thread(
                    _workflow_api_get,
                    server,
                    f"/workflows/{workflow_id}/result",
                    user_number,
                    session_id,
                    {},
                )
                content = result_payload.get("content")
                if isinstance(content, str):
                    result_text = content
            except Exception as exc:
                if workflow_details:
                    print(
                        f"WARNING: Full Workflow result unavailable: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
            if not result_text:
                result_text = _workflow_result_summary(status_payload)
            if result_text:
                print(f"\n{result_text}", flush=True)
            return True
        if status in {"failed", "cancelled"}:
            reason = workflow.get("terminal_reason_code") if isinstance(workflow, dict) else None
            print(
                f"ERROR: Workflow {workflow_id} ended with {status}"
                f"{f' ({reason})' if reason else ''}.",
                file=sys.stderr,
                flush=True,
            )
            return False
        if status in {"blocked", "waiting_input"}:
            waiting_input = workflow.get("waiting_input") if isinstance(workflow, dict) else None
            if status == "waiting_input" and interactive and isinstance(waiting_input, dict):
                resume_token = waiting_input.get("resume_token")
                if isinstance(resume_token, str) and resume_token:
                    required_fields = waiting_input.get("required_fields")
                    if isinstance(required_fields, list) and required_fields:
                        print("Workflow input required:", flush=True)
                        for field in required_fields:
                            print(f"- {field}", flush=True)
                    try:
                        response = await asyncio.to_thread(
                            input,
                            (
                                f"[workflow {workflow_id}]> "
                                if workflow_details
                                else "[研究补充信息]> "
                            ),
                        )
                    except EOFError:
                        print(flush=True)
                        return False
                    if not response.strip():
                        print("Workflow input cannot be empty.", file=sys.stderr, flush=True)
                        continue
                    try:
                        await asyncio.to_thread(
                            _workflow_api_post,
                            server,
                            f"/workflows/{workflow_id}/input",
                            user_number,
                            session_id,
                            {
                                "resume_token": resume_token,
                                "values": {"response": response.strip()},
                            },
                        )
                    except Exception as exc:
                        print(
                            f"ERROR: Could not submit Workflow input: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        return False
                    print(f"Workflow {workflow_id} resumed.", flush=True)
                    continue
            print(
                f"Workflow {workflow_id} is {status}: {waiting_input or 'input required'}.",
                file=sys.stderr,
                flush=True,
            )
            return False
        await asyncio.sleep(max(poll_seconds, 0.0))


def _workflow_api_get(
    server: str,
    path: str,
    user_number: str,
    session_id: str,
    query: dict[str, object],
) -> JsonObject:
    base = _http_server_base(server)
    parameters = {
        "user_id": user_number,
        "session_id": session_id,
        **query,
    }
    url = f"{base}{path}?{urlencode(parameters)}"
    with urllib.request.urlopen(url, timeout=10.0) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("workflow API returned a non-object response")
    return payload


def _workflow_api_post(
    server: str,
    path: str,
    user_number: str,
    session_id: str,
    body: JsonObject,
) -> JsonObject:
    base = _http_server_base(server)
    query = urlencode({"user_id": user_number, "session_id": session_id})
    request = urllib.request.Request(
        f"{base}{path}?{query}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10.0) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("workflow API returned a non-object response")
    return payload


def _http_server_base(server: str) -> str:
    base = server.rstrip("/")
    if base.startswith("ws://"):
        return "http://" + base.removeprefix("ws://")
    if base.startswith("wss://"):
        return "https://" + base.removeprefix("wss://")
    return base


def _print_workflow_event(workflow_id: str, event: JsonObject) -> None:
    cursor = event.get("cursor")
    event_type = event.get("event_type") or "workflow.event"
    status = event.get("status") or "unknown"
    print(
        f"[workflow {workflow_id} #{cursor}] {event_type}: {status}",
        flush=True,
    )


def project_workflow_progress(payload: JsonObject) -> JsonObject:
    """Project persisted Workflow state into a product-facing progress fact."""

    projected = payload.get("progress")
    if isinstance(projected, dict):
        return dict(projected)

    workflow = payload.get("workflow")
    plan = payload.get("plan")
    if not isinstance(workflow, dict) or not isinstance(plan, dict):
        return {}
    items = plan.get("work_items")
    if not isinstance(items, list):
        items = []
    normalized_items = [item for item in items if isinstance(item, dict)]
    completed = sum(
        item.get("status") in {"succeeded", "skipped", "superseded"}
        for item in normalized_items
    )
    status = str(workflow.get("status") or "unknown")
    active = next(
        (
            item
            for item in normalized_items
            if item.get("status") in {"running", "ready", "blocked"}
        ),
        None,
    )
    active_items = sorted(
        (
            item
            for item in normalized_items
            if item.get("status") in {"running", "blocked"}
        ),
        key=lambda item: str(item.get("work_item_id") or ""),
    )
    state = (
        "completed"
        if status == "completed"
        else "waiting_input"
        if status == "waiting_input"
        else "failed"
        if status in {"failed", "cancelled", "blocked"}
        else "working"
    )
    return {
        "state": state,
        "plan_kind": str(workflow.get("workflow_type") or "workflow"),
        "workflow_type": str(workflow.get("workflow_type") or "workflow"),
        "work_item_id": str(active.get("work_item_id") or "") if active else "",
        "work_item_kind": str(active.get("kind") or "") if active else "",
        "display_title": _safe_workflow_display_title(
            active.get("display_title") if active else None
        ),
        "completed_items": completed,
        "total_items": len(normalized_items),
        "attempt_count": int(active.get("attempt_count") or 0) if active else 0,
        "running_items": sum(
            item.get("status") == "running" for item in normalized_items
        ),
        "ready_items": sum(
            item.get("status") == "ready" for item in normalized_items
        ),
        "active_items": [
            {
                "work_item_id": str(item.get("work_item_id") or ""),
                "work_item_kind": str(item.get("kind") or ""),
                "display_title": _safe_workflow_display_title(
                    item.get("display_title")
                ),
                "attempt_count": int(item.get("attempt_count") or 0),
                "status": str(item.get("status") or ""),
            }
            for item in active_items
        ],
    }


_WORKFLOW_STAGE_LABELS = {
    "scope": "界定研究范围",
    "collect_sources": "收集并核实资料",
    "extract_evidence": "提取证据与分歧",
    "outline": "整理报告结构",
    "draft": "撰写研究报告",
    "verify": "核验引用与结论",
    "synthesize": "生成最终报告",
}


def _workflow_progress_message(progress: JsonObject) -> str:
    state = progress.get("state")
    completed = int(progress.get("completed_items") or 0)
    total = int(progress.get("total_items") or 0)
    if state == "completed":
        return f"研究完成（{completed}/{total} 个阶段）。"
    if state == "waiting_input":
        return f"研究需要补充信息（已完成 {completed}/{total} 个阶段）。"
    if state == "failed":
        return f"研究未能完成（已完成 {completed}/{total} 个阶段）。"
    active_items = progress.get("active_items")
    if isinstance(active_items, list):
        active_titles = [
            _safe_workflow_display_title(item.get("display_title"))
            for item in active_items
            if isinstance(item, dict) and item.get("status") == "running"
        ]
        active_titles = [title for title in active_titles if title]
        if len(active_titles) > 1:
            return (
                f"正在并行推进 {len(active_titles)} 个阶段："
                f"{'、'.join(active_titles)}（已完成 {completed}/{total}）。"
            )
    kind = str(progress.get("work_item_kind") or "")
    title = _safe_workflow_display_title(progress.get("display_title"))
    label = title or _WORKFLOW_STAGE_LABELS.get(kind, "推进研究任务")
    current = min(completed + 1, total) if total else 0
    attempt = int(progress.get("attempt_count") or 0)
    retry = f"，第 {attempt + 1} 次尝试" if attempt else ""
    prefix = "" if title else "当前阶段："
    return f"{prefix}{label}（{current}/{total}{retry}）。"


def _safe_workflow_display_title(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:160]


def _workflow_result_summary(payload: JsonObject) -> str:
    plan = payload.get("plan")
    items = plan.get("work_items") if isinstance(plan, dict) else None
    if not isinstance(items, list):
        return ""
    for item in reversed(items):
        if not isinstance(item, dict) or item.get("status") != "succeeded":
            continue
        summary = item.get("result_summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return ""


def _workflow_ids_from_chat_response_body(body: JsonObject) -> list[str]:
    output_refs = body.get("outputRefs")
    if not isinstance(output_refs, list):
        return []
    prefix = "workflow://"
    return list(dict.fromkeys(
        value.removeprefix(prefix)
        for value in output_refs
        if isinstance(value, str)
        and value.startswith(prefix)
        and len(value) > len(prefix)
    ))


def _task_ids_from_chat_response_body(body: JsonObject) -> list[str]:
    output_refs = body.get("outputRefs")
    if not isinstance(output_refs, list):
        return []
    prefix = "task://"
    return list(dict.fromkeys(
        value.removeprefix(prefix)
        for value in output_refs
        if isinstance(value, str)
        and value.startswith(prefix)
        and len(value) > len(prefix)
    ))


async def _send_chat_response_ack(
    websocket: Any,
    *,
    delivery_id: str,
    chat_index: str,
    session_id: str | None,
) -> None:
    await websocket.send(
        json.dumps(
            media_envelope(
                "chatResponseAck",
                {"deliveryId": delivery_id, "chatIndex": chat_index},
                session_id=session_id,
            ),
            ensure_ascii=False,
        )
    )
    await websocket.recv()


def agent_service_ws_url(server: str, *, session_id: str | None) -> str:
    """Return the Media-Agent compatible WebSocket URL."""

    base = server.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    query = f"?{urlencode({'sessionId': session_id})}" if session_id else ""
    return f"{base}/agent-service/v1{query}"


def media_envelope(
    message: str,
    body: JsonObject,
    *,
    session_id: str | None = None,
) -> JsonObject:
    """Build the vendor envelope with a JSON-string body."""

    envelope: JsonObject = {
        "message": message,
        "body": json.dumps(body, ensure_ascii=False),
    }
    if session_id:
        envelope["sessionId"] = session_id
    return envelope


def assistant_control_body(
    *,
    user_number: str,
    call_type: str,
    model_name: str | None,
    chat_progress: bool,
    chat_response_ack: bool,
    citations: bool = False,
) -> JsonObject:
    body: JsonObject = {
        "number": user_number,
        "callType": call_type,
        "clientInfo": {
            "clientType": "media_simulator",
            "clientName": "scripts/media_simulator.py",
        },
    }
    if model_name:
        body["modelName"] = model_name
    capabilities: JsonObject = {}
    if chat_progress:
        capabilities["chatProgress"] = True
    if chat_response_ack:
        capabilities["chatResponseAck"] = True
    if citations:
        capabilities["urlCitationAnnotationsV1"] = True
    if capabilities:
        body["clientCapabilities"] = capabilities
    return body


def chat_body(
    *,
    text: str,
    chat_index: str,
    user_number: str,
    speaker_number: str,
    stream: bool,
    assistant_mode: str = "standard",
    now: Callable[[], str] | None = None,
) -> JsonObject:
    timestamp = now() if now is not None else _now_iso()
    return {
        "chatIndex": chat_index,
        "userNumber": user_number,
        "contents": [
            {
                "speakerNumber": speaker_number,
                "speechContent": text,
                "time": timestamp,
            }
        ],
        "stream": stream,
        "assistantMode": assistant_mode,
    }


def parse_console_command(line: str) -> tuple[str, str | None]:
    stripped = line.strip()
    if not stripped:
        return "empty", None
    if not stripped.startswith("/"):
        return "chat", stripped
    command, _, value = stripped.partition(" ")
    normalized = command.lower()
    value = value.strip() or None
    if normalized in {"/quit", "/exit", "/q"}:
        return "quit", None
    if normalized in {"/new", "/session"}:
        return "new", value
    if normalized in {"/help", "/h", "/?"}:
        return "help", None
    if normalized == "/deep" and value is not None and value.lower() == "research":
        return "mode", "deep_research"
    if normalized in {"/deep_research", "/standard"}:
        return "mode", normalized.removeprefix("/")
    return "unknown", stripped


def parse_body(envelope: JsonObject) -> JsonObject:
    body = envelope.get("body")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return body if isinstance(body, dict) else {}


def chat_response_description(body: JsonObject) -> str | None:
    message = body.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, dict):
        intent_result = content.get("intentResult")
        if isinstance(intent_result, dict) and intent_result.get("description") is not None:
            return str(intent_result["description"])
    if isinstance(content, str):
        return content
    return None


def chat_response_error(body: JsonObject) -> str | None:
    if body.get("code") not in {"FAIL", -1}:
        return None
    message = body.get("message") or body.get("error") or "chat request failed"
    return str(message)


def _print_citation_sources(body: JsonObject) -> None:
    message = body.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    intent_result = content.get("intentResult") if isinstance(content, dict) else None
    annotations = intent_result.get("annotations") if isinstance(intent_result, dict) else None
    if not isinstance(annotations, list):
        return
    seen: set[str] = set()
    for item in annotations:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        marker = source_id.removeprefix("source_")
        title = str(item.get("title") or "source")
        url = str(item.get("url") or "")
        print(f"source [{marker}] {title} {url}", flush=True)


def print_json(value: JsonObject) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _print_protocol_error(body: JsonObject) -> None:
    message = body.get("message") or body.get("error") or body.get("code") or body
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


def _print_interrupted_turn(exc: BaseException) -> None:
    close_frame = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
    code = getattr(close_frame, "code", None)
    reason = getattr(close_frame, "reason", None)
    close_detail = ""
    if code is not None:
        close_detail = f" (code {code}"
        if reason:
            close_detail += f": {reason}"
        close_detail += ")"
    print(
        "ERROR: Server connection closed"
        f"{close_detail}. Reconnecting; the interrupted message was not retried. "
        "Please resend it.",
        file=sys.stderr,
        flush=True,
    )


def _print_reconnect_error(exc: BaseException) -> None:
    print(
        f"ERROR: Could not reconnect to the server: {exc}",
        file=sys.stderr,
        flush=True,
    )


def new_session_id() -> str:
    return new_prefixed_uuid7("media-simulator", separator="-")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a Media-Agent compatible WebSocket client against /agent-service/v1. "
            "Omit text to enter an interactive console."
        )
    )
    parser.add_argument("text", nargs="?", help="Optional first chat message.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="HTTP server base URL.")
    parser.add_argument("--session-id", default=None, help="Media sessionId. Defaults to a generated id.")
    parser.add_argument("--user-number", "--user-id", default="10086", help="Media userNumber/number.")
    parser.add_argument("--call-type", choices=("AUDIO", "VIDEO"), default="AUDIO")
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--stream", action="store_true", help="Request streaming chatResponse packets.")
    parser.add_argument("--chat-progress", action="store_true", help="Negotiate chatProgress packets.")
    parser.add_argument(
        "--chat-response-ack",
        action="store_true",
        help="Negotiate and send application-level chatResponseAck packets.",
    )
    parser.add_argument(
        "--citations",
        action="store_true",
        help="Negotiate URL citation annotations for the simulated media client.",
    )
    parser.add_argument(
        "--citation-debug",
        action="store_true",
        help="Print citation source diagnostics; also enables citation negotiation.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Keep the console open after sending the optional first text.",
    )
    parser.add_argument(
        "--workflow-details",
        action="store_true",
        help="Print raw durable Workflow events in addition to product progress.",
    )
    parser.add_argument(
        "--wait-proactive",
        action="store_true",
        help="After a durable task is created, wait for its proactive Agent-Service reminder.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(
        run_media_console(
            server=args.server,
            user_number=args.user_number,
            session_id=args.session_id,
            initial_text=args.text,
            call_type=args.call_type,
            model_name=args.model_name,
            stream=args.stream,
            chat_progress=args.chat_progress,
            chat_response_ack=args.chat_response_ack,
            citations=args.citations or args.citation_debug,
            citation_debug=args.citation_debug,
            interactive=args.interactive or args.text is None,
            workflow_details=args.workflow_details,
            wait_proactive=args.wait_proactive,
        )
    )


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _chat_index_from_chat_response_body(body: JsonObject) -> str | None:
    message = body.get("message")
    if isinstance(message, dict) and message.get("chatIndex") is not None:
        return str(message["chatIndex"])
    return None


def _intent_status(body: JsonObject) -> str | None:
    message = body.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    intent_result = content.get("intentResult")
    if isinstance(intent_result, dict) and intent_result.get("status") is not None:
        return str(intent_result["status"])
    return None


def _print_console_help(session_id: str, *, assistant_mode: str = "standard") -> None:
    print(
        "Type text and press Enter to send chat. "
        f"Current session: {session_id}. Assistant mode: {assistant_mode}. "
        "Commands: /deep research, /standard, /new [sessionId], "
        "/session <sessionId>, /help, /quit.",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
