#!/usr/bin/env python3
"""Interactive client for the native LangGraph Agent Server."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from langgraph_sdk import get_sync_client

from assistant_agent.agent_server.auth import delegated_identity_signature


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
        "assistant-native-v1",
        input={
            "messages": [{"role": "user", "content": text}],
            "execution_mode": mode,
        },
        context=_context(),
        multitask_strategy="enqueue",
    )
    print(_response_text(result))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    headers = {"x-assistant-user": args.identity}
    if args.token:
        headers.update(
            {
                "authorization": f"Bearer {args.token}",
                "x-assistant-signature": delegated_identity_signature(
                    secret=args.token,
                    identity=args.identity,
                ),
            }
        )
    client = get_sync_client(url=args.server, headers=headers, timeout=args.timeout)
    thread_id = _ensure_thread(client, args.thread_id)
    if not args.interactive:
        text = " ".join(args.text).strip()
        if not text:
            raise SystemExit("text is required unless --interactive is used")
        return _run_once(client, text=text, thread_id=thread_id, mode=args.assistant_mode)
    mode = args.assistant_mode
    print(f"Agent Server thread: {thread_id}")
    print("Commands: /fast, /planning, /new, /exit")
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
        _run_once(client, text=text, thread_id=thread_id, mode=mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Client for assistant_agent Agent Server.")
    parser.add_argument("text", nargs="*")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--identity", default="local-cli")
    parser.add_argument("--thread-id")
    parser.add_argument("--assistant-mode", choices=("fast", "planning"), default="fast")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--token")
    parser.add_argument("--interactive", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
