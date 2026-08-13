#!/usr/bin/env python3
"""Interactive client for the native LangGraph Agent Server."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from langgraph_sdk import get_sync_client


def _context(*, user_id: str, assistant_mode: str) -> dict[str, object]:
    return {
        "user_id": user_id,
        "tenant_id": "local-cli",
        "assistant_mode": assistant_mode,
        "entry_profile": "cli",
        "media_capabilities": [],
    }


def _response_text(state: Mapping[str, Any]) -> str:
    assistant_state = state.get("assistant_state")
    response = assistant_state.get("final_response") if isinstance(assistant_state, Mapping) else None
    text = response.get("message") if isinstance(response, Mapping) else None
    return text if isinstance(text, str) else ""


def _ensure_thread(client: Any, thread_id: str | None, user_id: str) -> str:
    thread = client.threads.create(
        thread_id=thread_id,
        if_exists="do_nothing",
        metadata={"user_id": user_id, "client": "agent_cli"},
    )
    return str(thread["thread_id"])


def _run_once(client: Any, *, text: str, user_id: str, thread_id: str, mode: str) -> int:
    result = client.runs.wait(
        thread_id,
        "assistant",
        input={"request_input": {"turn_origin_id": f"cli:{thread_id}", "text": text}},
        context=_context(user_id=user_id, assistant_mode=mode),
        multitask_strategy="enqueue",
    )
    print(_response_text(result))
    return 0


def main() -> int:
    args = build_parser().parse_args()
    headers = {"authorization": f"Bearer {args.token}"} if args.token else None
    client = get_sync_client(url=args.server, headers=headers, timeout=args.timeout)
    thread_id = _ensure_thread(client, args.thread_id, args.user_id)
    if not args.interactive:
        text = " ".join(args.text).strip()
        if not text:
            raise SystemExit("text is required unless --interactive is used")
        return _run_once(client, text=text, user_id=args.user_id, thread_id=thread_id, mode=args.assistant_mode)
    mode = args.assistant_mode
    print(f"Agent Server thread: {thread_id}")
    print("Commands: /standard, /deep research, /new, /exit")
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
        if text == "/standard":
            mode = "standard"
            continue
        if text == "/deep research":
            mode = "deep_research"
            continue
        if text == "/new":
            thread_id = _ensure_thread(client, None, args.user_id)
            print(f"Agent Server thread: {thread_id}")
            continue
        _run_once(client, text=text, user_id=args.user_id, thread_id=thread_id, mode=mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Client for assistant_agent Agent Server.")
    parser.add_argument("text", nargs="*")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--user-id", default="local-cli")
    parser.add_argument("--thread-id")
    parser.add_argument("--assistant-mode", choices=("standard", "deep_research"), default="standard")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--token")
    parser.add_argument("--interactive", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
