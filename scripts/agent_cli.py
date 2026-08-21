#!/usr/bin/env python3
"""Interactive client for the native LangGraph Agent Server."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any

from langgraph_sdk import get_sync_client

from assistant_agent.agent_server.client import (
    IncompatibleCheckpointGraphError,
    require_current_checkpoint_graph,
)
from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID

ASSISTANT_ID = ASSISTANT_GRAPH_ID


def _context() -> dict[str, object]:
    return {
        "entry_profile": "cli",
        "media_capabilities": [],
    }


def _response_text(state: Mapping[str, Any]) -> str:
    messages = state.get("messages")
    if not isinstance(messages, (list, tuple)):
        return ""
    for message in reversed(messages):
        if isinstance(message, Mapping):
            if message.get("role") != "assistant" and message.get("type") != "ai":
                continue
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _ensure_thread(client: Any, thread_id: str | None) -> str:
    thread = client.threads.create(
        thread_id=thread_id,
        if_exists="do_nothing",
        metadata={"client": "agent_cli"},
    )
    return str(thread["thread_id"])


def _run_once(client: Any, *, text: str, thread_id: str, mode: str) -> int:
    result = client.runs.wait(
        thread_id,
        ASSISTANT_ID,
        input={
            "messages": [{"role": "user", "content": text}],
            "execution_mode": mode,
        },
        context=_context(),
        multitask_strategy="enqueue",
    )
    print(_response_text(result))
    return 0


def _print_checkpoint_history(
    client: Any,
    *,
    thread_id: str,
    limit: int = 20,
) -> int:
    states = client.threads.get_history(thread_id, limit=limit)
    if not states:
        print("No checkpoints.")
        return 0
    for state in states:
        checkpoint = state.get("checkpoint")
        checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
        checkpoint_id = state.get("checkpoint_id") or checkpoint.get("checkpoint_id")
        metadata = state.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        next_nodes = state.get("next")
        next_nodes = next_nodes if isinstance(next_nodes, (list, tuple)) else ()
        print(
            " ".join(
                (
                    f"checkpoint_id={checkpoint_id or 'unknown'}",
                    f"step={metadata.get('step', 'unknown')}",
                    f"next={','.join(str(node) for node in next_nodes) or '-'}",
                    f"created_at={state.get('created_at', 'unknown')}",
                )
            )
        )
    return 0


def _replay_checkpoint(
    client: Any,
    *,
    thread_id: str,
    checkpoint_id: str,
    confirm: Callable[[str], str] = input,
) -> int:
    states = client.threads.get_history(thread_id, limit=100)
    selected = next(
        (
            state
            for state in states
            if _state_checkpoint_id(state) == checkpoint_id
        ),
        None,
    )
    if selected is None:
        print(f"Checkpoint not found: {checkpoint_id}")
        return 1
    try:
        require_current_checkpoint_graph(selected)
    except IncompatibleCheckpointGraphError as exc:
        print(f"Replay rejected: {exc}")
        return 1
    expected = f"REPLAY {checkpoint_id}"
    answer = confirm(
        "Replay re-executes nodes after the checkpoint and may repeat external "
        f"side effects. Type {expected!r} to continue: "
    )
    if answer != expected:
        print("Replay cancelled.")
        return 1
    result = client.runs.wait(
        thread_id,
        ASSISTANT_ID,
        input=None,
        checkpoint_id=checkpoint_id,
        context=_context(),
        multitask_strategy="enqueue",
    )
    print(_response_text(result))
    return 0


def _state_checkpoint_id(state: Mapping[str, Any]) -> str | None:
    checkpoint = state.get("checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    value = state.get("checkpoint_id") or checkpoint.get("checkpoint_id")
    return str(value) if value is not None else None


def _rollback_run(
    client: Any,
    *,
    thread_id: str,
    run_id: str,
    confirm: Callable[[str], str] = input,
) -> int:
    expected = f"ROLLBACK {run_id}"
    answer = confirm(
        "Rollback deletes the run and its checkpoints, but does not undo external "
        "tool side effects that already happened. "
        f"Type {expected!r} to continue: "
    )
    if answer != expected:
        print("Rollback cancelled.")
        return 1
    client.runs.cancel(
        thread_id,
        run_id,
        action="rollback",
        wait=True,
    )
    print(f"Rolled back run: {run_id}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    headers = {"x-assistant-user": args.identity}
    client = get_sync_client(url=args.server, headers=headers, timeout=args.timeout)
    thread_id = _ensure_thread(client, args.thread_id)
    if not args.interactive:
        text = " ".join(args.text).strip()
        if not text:
            raise SystemExit("text is required unless --interactive is used")
        return _run_once(client, text=text, thread_id=thread_id, mode=args.assistant_mode)
    mode = args.assistant_mode
    print(f"Agent Server thread: {thread_id}")
    print(
        "Commands: /fast, /planning, /new, /history, "
        "/replay <checkpoint_id>, /rollback <run_id>, /exit"
    )
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            return 0
        if text == "/fast":
            mode = "fast"
            continue
        if text == "/planning":
            mode = "planning"
            continue
        if text == "/new":
            thread_id = _ensure_thread(client, None)
            print(f"Agent Server thread: {thread_id}")
            continue
        if text == "/history":
            _print_checkpoint_history(client, thread_id=thread_id)
            continue
        if text == "/replay":
            print("Usage: /replay <checkpoint_id>")
            continue
        if text.startswith("/replay "):
            checkpoint_id = text.removeprefix("/replay ").strip()
            if not checkpoint_id:
                print("Usage: /replay <checkpoint_id>")
                continue
            _replay_checkpoint(
                client,
                thread_id=thread_id,
                checkpoint_id=checkpoint_id,
            )
            continue
        if text == "/rollback":
            print("Usage: /rollback <run_id>")
            continue
        if text.startswith("/rollback "):
            run_id = text.removeprefix("/rollback ").strip()
            if not run_id:
                print("Usage: /rollback <run_id>")
                continue
            _rollback_run(client, thread_id=thread_id, run_id=run_id)
            continue
        _run_once(client, text=text, thread_id=thread_id, mode=mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Client for assistant_agent Agent Server.")
    parser.add_argument("text", nargs="*")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--identity", default="local-cli")
    parser.add_argument("--thread-id")
    parser.add_argument("--assistant-mode", choices=("fast", "planning"), default="fast")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--interactive", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
