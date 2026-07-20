#!/usr/bin/env python3
"""CLI client for memory audit endpoints."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx


DEFAULT_SERVER = "http://127.0.0.1:8000"


class MemoryAuditClientError(RuntimeError):
    """Raised when the memory audit API request fails."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and manage agent memory through the FastAPI backend.")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Backend server base URL.")
    parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List memory items for a user.")
    list_parser.add_argument("--user-id", required=True)
    list_parser.add_argument("--memory-type", help="Optional memory_type filter.")
    list_parser.add_argument("--include-content", action="store_true")

    get_parser = subparsers.add_parser("get", help="Get one memory item.")
    get_parser.add_argument("--user-id", required=True)
    get_parser.add_argument("--memory-id", required=True)
    get_parser.add_argument("--no-content", action="store_true")

    audit_parser = subparsers.add_parser("audit", help="Generate a memory audit report.")
    audit_parser.add_argument("--user-id", required=True)

    delete_parser = subparsers.add_parser("delete", help="Delete one memory item.")
    delete_parser.add_argument("--user-id", required=True)
    delete_parser.add_argument("--memory-id", required=True)
    delete_parser.add_argument("--yes", action="store_true", help="Confirm deletion.")

    delete_session_parser = subparsers.add_parser("delete-session", help="Delete memories for one session.")
    delete_session_parser.add_argument("--user-id", required=True)
    delete_session_parser.add_argument("--session-id", required=True)
    delete_session_parser.add_argument("--yes", action="store_true", help="Confirm deletion.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"delete", "delete-session"} and not args.yes:
        print("Deletion requires --yes.", file=sys.stderr)
        return 2
    try:
        payload = run_command(args)
    except MemoryAuditClientError as exc:
        print(f"memory audit failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_human(payload, command=args.command)
    return 0


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "list":
        params: dict[str, Any] = {"include_content": args.include_content}
        if args.memory_type:
            params["memory_type"] = args.memory_type
        return request_json(args.server, "GET", f"/memory/users/{_q(args.user_id)}/items", params=params)
    if args.command == "get":
        return request_json(
            args.server,
            "GET",
            f"/memory/users/{_q(args.user_id)}/items/{_q(args.memory_id)}",
            params={"include_content": not args.no_content},
        )
    if args.command == "audit":
        return request_json(args.server, "GET", f"/memory/users/{_q(args.user_id)}/audit")
    if args.command == "delete":
        return request_json(args.server, "DELETE", f"/memory/users/{_q(args.user_id)}/items/{_q(args.memory_id)}")
    if args.command == "delete-session":
        return request_json(args.server, "DELETE", f"/memory/users/{_q(args.user_id)}/sessions/{_q(args.session_id)}")
    raise MemoryAuditClientError(f"Unsupported command: {args.command}")


def request_json(
    server: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    url = server.rstrip("/") + path
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.request(method, url, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MemoryAuditClientError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise MemoryAuditClientError("API returned a non-object JSON payload.")
    return payload


def print_human(payload: dict[str, Any], *, command: str) -> None:
    if command == "list":
        print(f"memory items: {payload.get('total', 0)}")
        for item in payload.get("items", []):
            print(f"- {item.get('memory_id')} [{item.get('memory_type')}] {item.get('summary')}")
        return
    if command == "audit":
        print(f"memory audit for {payload.get('user_id')}: {payload.get('total', 0)} items")
        print(f"by_type: {payload.get('by_type', {})}")
        print(f"warnings: {payload.get('warnings', [])}")
        return
    if command in {"delete", "delete-session"}:
        print(f"deleted: {payload.get('deleted', {})}")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _q(value: str) -> str:
    return quote(value, safe="")


if __name__ == "__main__":
    raise SystemExit(main())
