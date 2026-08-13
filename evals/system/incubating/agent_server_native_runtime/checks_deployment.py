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
from typing import Any
from urllib.request import urlopen

from langgraph_sdk import get_client


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


async def _probe(url: str) -> None:
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
