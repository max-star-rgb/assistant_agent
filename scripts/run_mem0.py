#!/usr/bin/env python3
"""Start and interactively manage the repository-local Mem0 stack."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
COMPOSE_FILE = REPO_ROOT / "docker" / "mem0" / "compose.yaml"
MEM0_BASE_URL = "http://127.0.0.1:8890"
HEALTH_URL = f"{MEM0_BASE_URL}/ready"
STARTUP_TIMEOUT_SECONDS = 600.0
POLL_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 5.0

InputFn = Callable[[str], str]
RequestFn = Callable[..., dict[str, object]]


class CommandError(ValueError):
    """A recoverable interactive-console command error."""


class _CommandParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CommandError(message)


def main() -> int:
    missing = [path for path in (ENV_FILE, COMPOSE_FILE) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"Required file not found: {path}", file=sys.stderr)
        return 2

    try:
        _compose(
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "mem0",
            "qdrant",
            timeout=STARTUP_TIMEOUT_SECONDS,
        )
        container_id = _compose_output("ps", "-q", "mem0")
        if not container_id:
            print("Mem0 container was not created.", file=sys.stderr)
            return 1

        status = _wait_until_healthy(container_id)
        if status != "healthy":
            print(
                f"Mem0 did not become healthy (container status: {status}).",
                file=sys.stderr,
            )
            _print_diagnostics_hint()
            return 1

        payload = _read_health()
        version = str(payload.get("version") or "unknown")
        framework = str(payload.get("framework") or "mem0")
        print(
            f"Mem0 ready: {HEALTH_URL} (framework {framework}, version {version})",
            flush=True,
        )
        console_status = _interactive_console()
        print("Mem0 and Qdrant will continue running after this console exits.")
        print(f"Stop them with: {_stop_command()}")
        return console_status
    except KeyboardInterrupt:
        print(
            "\nStartup wait interrupted; any containers already started remain running.",
            file=sys.stderr,
        )
        return 130
    except subprocess.TimeoutExpired:
        print("Timed out while starting Mem0.", file=sys.stderr)
        _print_diagnostics_hint()
        return 1
    except (OSError, subprocess.CalledProcessError, URLError, ValueError) as exc:
        print(f"Failed to start Mem0: {exc}", file=sys.stderr)
        _print_diagnostics_hint()
        return 1


def _compose(*args: str, timeout: float | None = None) -> None:
    subprocess.run(
        _compose_command(*args),
        cwd=REPO_ROOT,
        check=True,
        timeout=timeout,
    )


def _compose_output(*args: str) -> str:
    completed = subprocess.run(
        _compose_command(*args),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _compose_command(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "mem0",
        *args,
    ]


def _wait_until_healthy(container_id: str) -> str:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    status = "unknown"
    while time.monotonic() < deadline:
        status = _container_status(container_id)
        if status == "healthy":
            return status
        if status in {"dead", "exited", "unhealthy"}:
            return status
        time.sleep(POLL_INTERVAL_SECONDS)
    return status


def _container_status(container_id: str) -> str:
    completed = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container_id,
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _read_health() -> dict[str, object]:
    with urlopen(HEALTH_URL, timeout=5.0) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError(f"unexpected health payload: {payload!r}")
    return payload


def _interactive_console(
    *,
    input_fn: InputFn = input,
    request_fn: RequestFn | None = None,
) -> int:
    request = request_fn or _mem0_request
    print("Interactive Mem0 console ready. Type 'help' for commands.")
    while True:
        try:
            line = input_fn("mem0> ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\nConsole interrupted; no containers were stopped.")
            return 0

        if not line.strip():
            continue
        try:
            args = shlex.split(line)
            if not _execute_command(args, input_fn=input_fn, request_fn=request):
                return 0
        except KeyboardInterrupt:
            print("\nCommand cancelled.", file=sys.stderr)
        except (ValueError, OSError) as exc:
            print(f"Command failed: {exc}", file=sys.stderr)


def _execute_command(
    args: list[str],
    *,
    input_fn: InputFn = input,
    request_fn: RequestFn | None = None,
) -> bool:
    if not args:
        return True
    request = request_fn or _mem0_request
    command, command_args = args[0].lower(), args[1:]

    if command in {"exit", "quit"}:
        _reject_extra_args(command, command_args)
        return False
    if command == "help":
        _reject_extra_args(command, command_args)
        print(_help_text())
        return True
    if command == "status":
        _reject_extra_args(command, command_args)
        _print_json(request("GET", "/ready"))
        return True
    if command == "list":
        parsed = _parse_list_args(command_args)
        query = {"limit": str(parsed.limit)} if parsed.limit is not None else None
        payload = request("GET", "/memories", query=query)
        _print_memory_list(payload)
        return True
    if command == "get":
        parsed = _parse_id_args("get", command_args)
        memory_id = parsed.memory_id or _required_input(input_fn, "Memory ID: ")
        _print_json(request("GET", _memory_path(memory_id)))
        return True
    if command == "history":
        parsed = _parse_id_args("history", command_args)
        memory_id = parsed.memory_id or _required_input(input_fn, "Memory ID: ")
        _print_json(request("GET", f"{_memory_path(memory_id)}/history"))
        return True
    if command == "add":
        parsed = _parse_add_args(command_args)
        identity = {
            "user_id": parsed.user_id,
            "agent_id": parsed.agent_id,
            "run_id": parsed.run_id,
        }
        if not any(identity.values()):
            print("Enter at least one identity; blank fields are omitted.")
            identity = {
                "user_id": input_fn("User ID (optional): ").strip() or None,
                "agent_id": input_fn("Agent ID (optional): ").strip() or None,
                "run_id": input_fn("Run ID (optional): ").strip() or None,
            }
        if not any(identity.values()):
            raise CommandError("add requires user_id, agent_id, or run_id")
        memory_text = " ".join(parsed.text).strip()
        if not memory_text:
            memory_text = _required_input(input_fn, "Memory text: ")
        body: dict[str, object] = {
            "memory": memory_text,
            "infer": parsed.infer,
            **{key: value for key, value in identity.items() if value},
        }
        _print_json(request("POST", "/memories", body=body))
        return True
    if command == "update":
        parsed = _parse_update_args(command_args)
        memory_id = parsed.memory_id or _required_input(input_fn, "Memory ID: ")
        memory_text = " ".join(parsed.text).strip()
        if not memory_text:
            memory_text = _required_input(input_fn, "New memory text: ")
        _print_json(
            request("PUT", _memory_path(memory_id), body={"memory": memory_text})
        )
        return True
    if command == "delete":
        parsed = _parse_delete_args(command_args)
        memory_id = parsed.memory_id or _required_input(input_fn, "Memory ID: ")
        path = _memory_path(memory_id)
        if not parsed.yes:
            print("Target memory:")
            _print_json(request("GET", path))
            confirmation = input_fn("Type 'yes' to delete: ").strip().lower()
            if confirmation != "yes":
                print("Delete cancelled.")
                return True
        _print_json(request("DELETE", path))
        return True

    raise CommandError(f"unknown command {command!r}; type 'help' for commands")


def _mem0_request(
    method: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    body: Mapping[str, object] | None = None,
) -> dict[str, object]:
    url = MEM0_BASE_URL + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode("utf-8")
    try:
        with urlopen(
            Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method=method,
            ),
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            payload: Any = json.load(response)
    except HTTPError as exc:
        raise CommandError(f"Mem0 HTTP {exc.code} {exc.reason}") from exc
    except URLError as exc:
        raise CommandError("Mem0 is unavailable on 127.0.0.1:8890") from exc
    except json.JSONDecodeError as exc:
        raise CommandError("Mem0 returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CommandError("Mem0 returned a non-object JSON response")
    return payload


def _parse_list_args(args: list[str]) -> argparse.Namespace:
    parser = _parser("list")
    parser.add_argument("--limit", type=_memory_limit)
    return parser.parse_args(args)


def _parse_id_args(command: str, args: list[str]) -> argparse.Namespace:
    parser = _parser(command)
    parser.add_argument("memory_id", nargs="?")
    return parser.parse_args(args)


def _parse_add_args(args: list[str]) -> argparse.Namespace:
    parser = _parser("add")
    parser.add_argument("--infer", action="store_true")
    parser.add_argument("--user-id")
    parser.add_argument("--agent-id")
    parser.add_argument("--run-id")
    parser.add_argument("text", nargs="*")
    return parser.parse_intermixed_args(args)


def _parse_update_args(args: list[str]) -> argparse.Namespace:
    parser = _parser("update")
    parser.add_argument("memory_id", nargs="?")
    parser.add_argument("text", nargs="*")
    return parser.parse_args(args)


def _parse_delete_args(args: list[str]) -> argparse.Namespace:
    parser = _parser("delete")
    parser.add_argument("memory_id", nargs="?")
    parser.add_argument("--yes", action="store_true")
    return parser.parse_intermixed_args(args)


def _parser(command: str) -> _CommandParser:
    return _CommandParser(prog=command, add_help=False, exit_on_error=False)


def _memory_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= value <= 50:
        raise argparse.ArgumentTypeError("limit must be between 1 and 50")
    return value


def _memory_path(memory_id: str) -> str:
    return f"/memories/{urllib.parse.quote(memory_id, safe='')}"


def _required_input(input_fn: InputFn, prompt: str) -> str:
    value = input_fn(prompt).strip()
    if not value:
        raise CommandError(f"{prompt.rstrip(': ')} is required")
    return value


def _reject_extra_args(command: str, args: list[str]) -> None:
    if args:
        raise CommandError(f"{command} does not accept arguments")


def _print_memory_list(payload: Mapping[str, object]) -> None:
    results = payload.get("results")
    if not isinstance(results, list):
        raise CommandError("Mem0 list response is missing results")
    print(f"Memories: {len(results)}")
    for index, item in enumerate(results, start=1):
        print(f"{index}. {json.dumps(item, ensure_ascii=False, sort_keys=True)}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _help_text() -> str:
    return """Commands:
  help
      Show this help.
  status
      Show Mem0 health information.
  list [--limit N]
      List raw memories for all users. N must be between 1 and 50.
  get [memory_id]
      Show one raw memory. Missing ID is prompted.
  history [memory_id]
      Show one memory's native history. Missing ID is prompted.
  add [--infer] [--user-id ID] [--agent-id ID] [--run-id ID] [--] [text]
      Add a memory. Text and identities are prompted when needed.
      Text is stored directly by default; --infer enables Mem0 inference.
  update [memory_id] [new text]
      Replace one memory's text. Missing values are prompted.
  delete [memory_id] [--yes]
      Delete one memory. Without --yes, show it and require confirmation.
  exit | quit
      Exit this console without stopping Mem0 or Qdrant.

Examples:
  list --limit 10
  add --user-id usr_example -- 我喜欢黑咖啡
  add --infer --user-id usr_example -- 用户说他喜欢黑咖啡
  update 01234567-89ab-cdef-0123-456789abcdef 新的记忆正文
  delete 01234567-89ab-cdef-0123-456789abcdef"""


def _print_diagnostics_hint() -> None:
    print(f"Inspect status with: {_status_command()}", file=sys.stderr)
    print(f"Inspect logs with: {_logs_command()}", file=sys.stderr)


def _status_command() -> str:
    return _display_command("ps")


def _logs_command() -> str:
    return _display_command("logs", "--tail", "100", "mem0", "qdrant")


def _stop_command() -> str:
    return _display_command("stop", "mem0", "qdrant")


def _display_command(*args: str) -> str:
    return (
        "docker compose --env-file .env "
        "-f docker/mem0/compose.yaml --profile mem0 "
        + " ".join(args)
    )


if __name__ == "__main__":
    raise SystemExit(main())
