from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from urllib.parse import urlparse

from websockets.exceptions import ConnectionClosed, ConnectionClosedError
from websockets.frames import Close

import scripts.run_client as run_client
from scripts.run_client import run_media_console


class _ScriptedSocket:
    def __init__(
        self,
        *,
        responses: list[str],
        fail_first_chat: BaseException | None = None,
    ) -> None:
        self.responses = iter(responses)
        self.fail_first_chat = fail_first_chat
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        envelope = json.loads(raw)
        self.sent.append(envelope)
        if envelope["message"] == "chat" and self.fail_first_chat is not None:
            error = self.fail_first_chat
            self.fail_first_chat = None
            raise error

    async def recv(self) -> str:
        return next(self.responses)

    async def close(self) -> None:
        self.closed = True


def _control_response() -> str:
    return json.dumps({"message": "assistantControlResponse", "body": "{}"})


def _chat_response(
    chat_index: str,
    description: str,
    *,
    output_refs: list[str] | None = None,
) -> str:
    body = {
        "code": "SUCCESS",
        "final": True,
        "message": {
            "chatIndex": chat_index,
            "content": {
                "intentResult": {
                    "status": "SUCCESS",
                    "description": description,
                }
            },
        },
    }
    if output_refs is not None:
        body["outputRefs"] = output_refs
    return json.dumps(
        {
            "message": "chatResponse",
            "body": json.dumps(body),
        }
    )


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_workflow_progress_is_projected_from_plan_state() -> None:
    projection = run_client.project_workflow_progress({
        "workflow": {
            "workflow_id": "workflow-sentinel",
            "workflow_type": "deep_research",
            "status": "running",
            "phase": "collect_sources",
        },
        "plan": {
            "work_items": [
                {"work_item_id": "scope", "kind": "scope", "status": "succeeded"},
                {
                    "work_item_id": "collect_sources",
                    "kind": "collect_sources",
                    "display_title": "正在检索并核实 Hermes 工程资料",
                    "status": "ready",
                },
                {"work_item_id": "synthesize", "kind": "synthesize", "status": "pending"},
            ]
        },
    })

    assert projection == {
        "state": "working",
        "plan_kind": "deep_research",
        "workflow_type": "deep_research",
        "work_item_id": "collect_sources",
        "work_item_kind": "collect_sources",
        "display_title": "正在检索并核实 Hermes 工程资料",
        "completed_items": 1,
        "total_items": 3,
        "attempt_count": 0,
    }


def test_interactive_client_reconnects_without_replaying_an_ambiguous_turn(
    monkeypatch,
    capsys,
) -> None:
    restart_error = ConnectionClosedError(
        Close(1012, "service restart"),
        Close(1012, "service restart"),
        True,
    )
    first_socket = _ScriptedSocket(
        responses=[_control_response()],
        fail_first_chat=restart_error,
    )
    second_socket = _ScriptedSocket(
        responses=[_control_response(), _chat_response("chat-2", "ok-sentinel")],
    )
    sockets = iter([first_socket, second_socket])
    connected_urls: list[str] = []

    async def connect(url: str, **_kwargs):
        connected_urls.append(url)
        return next(sockets)

    fake_websockets = SimpleNamespace(
        connect=connect,
        exceptions=SimpleNamespace(ConnectionClosed=ConnectionClosed),
    )
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    inputs = iter(["/deep research", "ambiguous-query", "second-query"])

    def scripted_input(_prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", scripted_input)

    result = asyncio.run(
        run_media_console(
            server="http://gateway-sentinel:8089",
            user_number="user-sentinel",
            session_id="session-sentinel",
            initial_text=None,
            stream=True,
            chat_progress=True,
            chat_response_ack=False,
            interactive=True,
        )
    )

    assert result == 0
    assert len(connected_urls) == 2
    assert first_socket.closed is True
    second_chat_frames = [
        envelope for envelope in second_socket.sent if envelope["message"] == "chat"
    ]
    assert len(second_chat_frames) == 1
    second_chat_body = json.loads(str(second_chat_frames[0]["body"]))
    assert second_chat_body["contents"][0]["speechContent"] == "second-query"
    assert second_chat_body["assistantMode"] == "deep_research"
    assert "was not retried" in capsys.readouterr().err


def test_client_tails_structured_workflow_ref_until_completed(
    monkeypatch,
    capsys,
) -> None:
    socket = _ScriptedSocket(
        responses=[
            _control_response(),
            _chat_response(
                "chat-1",
                "accepted-sentinel",
                output_refs=["workflow://workflow-sentinel"],
            ),
        ]
    )

    async def connect(_url: str, **_kwargs):
        return socket

    fake_websockets = SimpleNamespace(
        connect=connect,
        exceptions=SimpleNamespace(ConnectionClosed=ConnectionClosed),
    )
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    requested_paths: list[str] = []

    def urlopen(url: str, *, timeout: float):
        assert timeout > 0
        path = urlparse(url).path
        requested_paths.append(path)
        if path.endswith("/events"):
            return _JsonResponse(
                {
                    "workflow_id": "workflow-sentinel",
                    "events": [
                        {
                            "cursor": 1,
                            "event_type": "workflow.accepted",
                            "status": "queued",
                            "payload": {},
                        },
                        {
                            "cursor": 2,
                            "event_type": "workflow.completed",
                            "status": "completed",
                            "payload": {},
                        },
                    ],
                    "next_cursor": 2,
                }
            )
        return _JsonResponse(
            {
                "workflow": {
                    "workflow_id": "workflow-sentinel",
                    "status": "completed",
                    "phase": "completed",
                    "waiting_input": None,
                },
                "plan": {
                    "work_items": [
                        {
                            "work_item_id": "draft-sentinel",
                            "status": "succeeded",
                            "result_summary": "final-report-sentinel",
                        }
                    ]
                },
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    result = asyncio.run(
        run_media_console(
            server="http://gateway-sentinel:8089",
            user_number="user-sentinel",
            session_id="session-sentinel",
            initial_text="research-sentinel",
            stream=True,
            chat_progress=True,
            chat_response_ack=False,
            interactive=False,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert requested_paths == [
        "/workflows/workflow-sentinel/events",
        "/workflows/workflow-sentinel",
    ]
    assert "workflow.accepted" not in output
    assert "workflow.completed" not in output
    assert "研究完成" in output
    assert "final-report-sentinel" in output


def test_interactive_client_submits_waiting_workflow_input_instead_of_new_chat(
    monkeypatch,
) -> None:
    socket = _ScriptedSocket(
        responses=[
            _control_response(),
            _chat_response(
                "chat-1",
                "accepted-sentinel",
                output_refs=["workflow://workflow-sentinel"],
            ),
        ]
    )

    async def connect(_url: str, **_kwargs):
        return socket

    fake_websockets = SimpleNamespace(
        connect=connect,
        exceptions=SimpleNamespace(ConnectionClosed=ConnectionClosed),
    )
    monkeypatch.setitem(sys.modules, "websockets", fake_websockets)
    inputs = iter(["clarification-sentinel"])

    def scripted_input(_prompt: str) -> str:
        try:
            return next(inputs)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", scripted_input)
    status_reads = 0
    posted_payloads: list[dict[str, object]] = []

    def urlopen(target, *, timeout: float):
        nonlocal status_reads
        assert timeout > 0
        url = target.full_url if hasattr(target, "full_url") else target
        path = urlparse(url).path
        if getattr(target, "data", None) is not None:
            posted_payloads.append(json.loads(target.data))
            return _JsonResponse({
                "workflow": {
                    "workflow_id": "workflow-sentinel",
                    "status": "queued",
                    "phase": "resumed",
                    "waiting_input": None,
                },
                "plan": {"work_items": []},
            })
        if path.endswith("/events"):
            return _JsonResponse({
                "workflow_id": "workflow-sentinel",
                "events": [],
                "next_cursor": 0,
            })
        status_reads += 1
        if status_reads == 1:
            return _JsonResponse({
                "workflow": {
                    "workflow_id": "workflow-sentinel",
                    "status": "waiting_input",
                    "phase": "waiting_input",
                    "waiting_input": {
                        "required_fields": ["framework-selection"],
                        "resume_token": "resume-sentinel",
                    },
                },
                "plan": {"work_items": []},
            })
        return _JsonResponse({
            "workflow": {
                "workflow_id": "workflow-sentinel",
                "status": "completed",
                "phase": "completed",
                "waiting_input": None,
            },
            "plan": {
                "work_items": [{
                    "work_item_id": "synthesize-sentinel",
                    "status": "succeeded",
                    "result_summary": "final-report-sentinel",
                }]
            },
        })

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    result = asyncio.run(
        run_media_console(
            server="http://gateway-sentinel:8089",
            user_number="user-sentinel",
            session_id="session-sentinel",
            initial_text="research-sentinel",
            stream=True,
            chat_progress=True,
            chat_response_ack=False,
            interactive=True,
        )
    )

    assert result == 0
    assert posted_payloads == [{
        "resume_token": "resume-sentinel",
        "values": {"response": "clarification-sentinel"},
    }]
    assert len([item for item in socket.sent if item["message"] == "chat"]) == 1
