"""Run a real local Agent Server deployment probe with mock providers only."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib.request import urlopen

from langgraph_sdk import get_client
import websockets


REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON = Path("/home/lenovo1/miniconda3/envs/hello_agent/bin/python")
LANGGRAPH = PYTHON.with_name("langgraph")


def _emit(check: str, passed: bool, **details: Any) -> None:
    print(
        json.dumps(
            {"check": check, "status": "PASS" if passed else "FAIL", **details},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    if not passed:
        raise AssertionError(check)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(f"Agent Server exited during startup: {output[-4000:]}")
        try:
            with urlopen(f"{url}/ok", timeout=1) as response:  # noqa: S310 - loopback
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError("Agent Server did not become ready within 30 seconds")


def _context(user_id: str) -> dict[str, object]:
    return {
        "user_id": user_id,
        "tenant_id": "probe-tenant",
        "assistant_mode": "standard",
        "entry_profile": "system_probe",
        "media_capabilities": [],
    }


async def _probe(url: str) -> str:
    client = get_client(url=url)
    assistants = await client.assistants.search(graph_id="assistant")
    assistant = assistants[0]
    assistant_id = str(assistant["assistant_id"])
    schemas = await client.assistants.get_schemas(assistant_id)
    input_schema = schemas["input_schema"]
    request_schema = input_schema["$defs"]["AgentServerGraphInput"]
    _emit(
        "assistant_schema",
        set(request_schema["required"]) == {"turn_origin_id", "text"},
        assistant_id=assistant_id,
    )

    first = await client.threads.create(metadata={"probe_user": "probe-user-a"})
    second = await client.threads.create(metadata={"probe_user": "probe-user-b"})
    first_id = str(first["thread_id"])
    second_id = str(second["thread_id"])
    _emit("thread_isolation", first_id != second_id, thread_count=2)

    result = await client.runs.wait(
        first_id,
        "assistant",
        input={
            "request_input": {
                "turn_origin_id": "deployment-probe-turn",
                "text": "你好",
            }
        },
        context=_context("probe-user-a"),
    )
    state = result["assistant_state"]
    _emit(
        "native_run_terminal",
        state["run"]["status"] == "completed"
        and state["request"]["session_id"] == first_id
        and state["turn_origin_id"] == "deployment-probe-turn"
        and state["response_publish"]["status"] == "published"
        and state["memory_commit"]["status"] == "skipped"
        and state["continuation"] == "end",
        run_id=state["run"]["run_id"],
    )
    snapshot = await client.threads.get_state(first_id, subgraphs=True)
    _emit(
        "checkpoint_state",
        snapshot["values"]["assistant_state"]["run"]["run_id"]
        == state["run"]["run_id"],
    )

    namespace = ("agent_server_probe", "probe-user-a")
    await client.store.put_item(namespace, "probe-key", {"value": "probe-value"})
    item = await client.store.get_item(namespace, "probe-key")
    _emit("native_store", item["value"] == {"value": "probe-value"})

    delayed = await client.runs.create(
        second_id,
        "assistant",
        input={
            "request_input": {
                "turn_origin_id": "deployment-probe-cancel",
                "text": "cancel me",
            }
        },
        context=_context("probe-user-b"),
        after_seconds=10,
    )
    delayed_id = str(delayed["run_id"])
    await client.runs.cancel(second_id, delayed_id, wait=True)
    cancelled = await client.runs.get(second_id, delayed_id)
    _emit("native_cancel", cancelled["status"] in {"interrupted", "error"})

    await _probe_enqueue(client)
    await _probe_resumable_thread_stream(client)

    await _probe_media_route(url)
    return first_id


async def _probe_enqueue(client: Any) -> None:
    thread = await client.threads.create(metadata={"probe": "enqueue"})
    thread_id = str(thread["thread_id"])
    first, second = await asyncio.gather(
        client.runs.create(
            thread_id,
            "assistant",
            input={"request_input": {"turn_origin_id": "enqueue-1", "text": "one"}},
            context=_context("probe-enqueue"),
            multitask_strategy="enqueue",
        ),
        client.runs.create(
            thread_id,
            "assistant",
            input={"request_input": {"turn_origin_id": "enqueue-2", "text": "two"}},
            context=_context("probe-enqueue"),
            multitask_strategy="enqueue",
        ),
    )
    await asyncio.gather(
        client.runs.join(thread_id, str(first["run_id"])),
        client.runs.join(thread_id, str(second["run_id"])),
    )
    runs = await client.runs.list(thread_id, limit=10)
    statuses = {str(run["run_id"]): run["status"] for run in runs}
    _emit(
        "native_enqueue",
        statuses.get(str(first["run_id"])) == "success"
        and statuses.get(str(second["run_id"])) == "success",
        run_count=len(runs),
    )


async def _probe_resumable_thread_stream(client: Any) -> None:
    thread = await client.threads.create(metadata={"probe": "resumable-stream"})
    thread_id = str(thread["thread_id"])
    first_event_id: str | None = None
    run_id: str | None = None
    async for part in client.runs.stream(
        thread_id,
        "assistant",
        input={
            "request_input": {
                "turn_origin_id": "resumable-stream",
                "text": "resume me",
            }
        },
        context=_context("probe-stream"),
        stream_mode=["values"],
        stream_resumable=True,
        on_disconnect="continue",
    ):
        first_event_id = part.id
        if part.event == "metadata" and isinstance(part.data, dict):
            run_id = str(part.data["run_id"])
        break
    if first_event_id is None or run_id is None:
        _emit("native_resumable_stream", False, reason="missing initial event")
        return
    await client.runs.join(thread_id, run_id)
    resumed = await client.threads.join_stream(
        thread_id,
        last_event_id=first_event_id,
        stream_mode="run_modes",
    )
    resumed_ids: list[str] = []
    saw_terminal = False
    async for part in resumed:
        if part.id is not None:
            resumed_ids.append(str(part.id))
        if (
            part.event == "metadata"
            and isinstance(part.data, dict)
            and part.data.get("status") == "run_done"
            and str(part.data.get("run_id")) == run_id
        ):
            saw_terminal = True
            break
    _emit(
        "native_resumable_stream",
        saw_terminal
        and bool(resumed_ids)
        and first_event_id not in resumed_ids,
        resumed_event_count=len(resumed_ids),
    )


async def _probe_media_route(url: str) -> None:
    websocket_url = url.replace("http://", "ws://", 1) + "/agent-service/v1"
    async with websockets.connect(websocket_url) as websocket:
        await websocket.send(
            _media_frame(
                "assistantControl",
                {"number": "probe-media-user", "callType": "AUDIO"},
            )
        )
        control = json.loads(await websocket.recv())
        await websocket.send(
            _media_frame(
                "chat",
                {
                    "chatIndex": "probe-chat-1",
                    "userNumber": "probe-media-user",
                    "contents": [
                        {
                            "speakerNumber": "probe-media-user",
                            "time": "1",
                            "speechContent": "你好",
                        }
                    ],
                    "stream": True,
                },
            )
        )
        progress = json.loads(await websocket.recv())
        final = json.loads(await websocket.recv())
    control_body = json.loads(control["body"])
    final_body = json.loads(final["body"])
    _emit(
        "media_native_route",
        control_body["code"] == 0
        and progress["message"] == "chatProgress"
        and final["message"] == "chatResponse"
        and final_body["message"]["content"]["intentResult"]["status"] == "SUCCESS",
    )


def _media_frame(message: str, body: Mapping[str, object]) -> str:
    return json.dumps(
        {"message": message, "sessionId": "probe-media-session", "body": json.dumps(body)}
    )


def main() -> int:
    if os.environ.get("MULTIMODAL_AGENT_PROVIDER_MODE", "mock") != "mock":
        raise RuntimeError("Deployment probe requires mock provider mode")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["MULTIMODAL_AGENT_PROVIDER_MODE"] = "mock"
    env["LANGSMITH_TRACING"] = "false"
    process = subprocess.Popen(
        [
            str(LANGGRAPH),
            "dev",
            "--no-browser",
            "--no-reload",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_ready(url, process)
        _emit("server_ready", True, url=url)
        asyncio.run(_probe(url))
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
