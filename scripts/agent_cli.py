#!/usr/bin/env python3
"""Interactive HTTP/SSE product client for assistant_agent."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from assistant_agent.clients.http_agent import HttpAgentClient, HttpAgentClientError
from assistant_agent.identifiers import new_prefixed_uuid7


def main() -> int:
    args = build_parser().parse_args()
    client = HttpAgentClient(server=args.server, timeout_s=args.timeout)
    session_id = args.session_id or new_prefixed_uuid7("cli-session", separator="-")
    if args.interactive:
        return _interactive(
            client,
            user_id=args.user_id,
            session_id=session_id,
            assistant_mode=args.assistant_mode,
            stream=not args.no_stream,
        )
    text = " ".join(args.text).strip()
    if not text:
        raise SystemExit("text is required unless --interactive is used")
    return _run_once(
        client,
        text=text,
        user_id=args.user_id,
        session_id=session_id,
        assistant_mode=args.assistant_mode,
        stream=not args.no_stream,
    )


def _interactive(
    client: HttpAgentClient,
    *,
    user_id: str,
    session_id: str,
    assistant_mode: str,
    stream: bool,
) -> int:
    mode = assistant_mode
    print(f"Agent CLI session: {session_id}")
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
            print("assistant mode: standard")
            continue
        if text == "/deep research":
            mode = "deep_research"
            print("assistant mode: deep_research")
            continue
        if text == "/new":
            session_id = new_prefixed_uuid7("cli-session", separator="-")
            print(f"Agent CLI session: {session_id}")
            continue
        _run_once(
            client,
            text=text,
            user_id=user_id,
            session_id=session_id,
            assistant_mode=mode,
            stream=stream,
        )


def _run_once(
    client: HttpAgentClient,
    *,
    text: str,
    user_id: str,
    session_id: str,
    assistant_mode: str,
    stream: bool,
) -> int:
    request = {
        "user_id": user_id,
        "session_id": session_id,
        "text": text,
        "assistant_mode": assistant_mode,
    }
    try:
        if stream:
            return _run_stream(
                client,
                request=request,
                user_id=user_id,
                session_id=session_id,
            )
        response = client.run_json(request)
        print(response.get("response_text") or "")
        _print_sources(response)
        return 0 if response.get("status") != "failed" else 1
    except HttpAgentClientError as exc:
        print(f"HTTP {exc.status_code}: {exc.detail}")
        return 1


def _run_stream(
    client: HttpAgentClient,
    *,
    request: Mapping[str, Any],
    user_id: str,
    session_id: str,
) -> int:
    run_id: str | None = None
    streamed = ""
    try:
        for event in client.run_stream(request):
            if event.event == "run.started":
                value = event.data.get("run_id")
                run_id = value if isinstance(value, str) else None
            elif event.event == "response.delta":
                delta = event.data.get("delta")
                if isinstance(delta, str) and delta:
                    print(delta, end="", flush=True)
                    streamed += delta
            elif event.event == "response.completed":
                text = event.data.get("response_text")
                terminal_text = text if isinstance(text, str) else ""
                remaining = (
                    terminal_text[len(streamed):]
                    if terminal_text.startswith(streamed)
                    else (terminal_text if not streamed else "")
                )
                if remaining:
                    print(remaining, end="", flush=True)
                print()
                _print_sources(event.data)
                return 0
            elif event.event in {"run.failed", "run.cancelled"}:
                if streamed:
                    print()
                print(f"{event.event}: {event.data}")
                return 1
    except KeyboardInterrupt:
        if run_id:
            client.cancel(
                run_id=run_id,
                user_id=user_id,
                session_id=session_id,
            )
        print("\ncancel requested")
        return 130
    return 1


def _print_sources(response: Mapping[str, Any]) -> None:
    annotations = response.get("annotations")
    if not isinstance(annotations, list):
        return
    seen: set[str] = set()
    for item in annotations:
        if not isinstance(item, Mapping):
            continue
        source_id = str(item.get("source_id") or "")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        title = str(item.get("title") or "source")
        url = str(item.get("url") or "")
        print(f"source {source_id.removeprefix('source_')}: {title} {url}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HTTP/SSE client for assistant_agent.")
    parser.add_argument("text", nargs="*", help="One non-interactive user message.")
    parser.add_argument("--server", default="http://127.0.0.1:8089")
    parser.add_argument("--user-id", default="local-cli")
    parser.add_argument("--session-id")
    parser.add_argument("--assistant-mode", choices=("standard", "deep_research"), default="standard")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--no-stream", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
