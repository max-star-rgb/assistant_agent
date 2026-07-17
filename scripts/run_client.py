"""Media-Agent protocol console client for /agent-service/v1."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

JsonObject = dict[str, Any]


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
    interactive: bool = False,
) -> int:
    """Run a Media-Agent compatible client.

    When ``initial_text`` is provided and ``interactive`` is false, this sends one
    chat message and exits. Otherwise it stays in a console loop.
    """

    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional operator dependency.
        raise RuntimeError("Install websockets to use scripts/run_client.py") from exc

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
    )
    chat_counter = 0
    try:
        if initial_text:
            chat_counter += 1
            ok = await _send_chat_and_print_responses(
                websocket,
                text=initial_text,
                chat_index=f"chat-{chat_counter}",
                user_number=user_number,
                session_id=current_session_id,
                stream=stream,
                chat_response_ack=chat_response_ack,
            )
            if not interactive:
                return 0 if ok else 1

        _print_console_help(current_session_id)
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
                _print_console_help(current_session_id)
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
                )
                chat_counter = 0
                print(f"Opened session {current_session_id}.", flush=True)
                continue

            chat_counter += 1
            await _send_chat_and_print_responses(
                websocket,
                text=str(value),
                chat_index=f"chat-{chat_counter}",
                user_number=user_number,
                session_id=current_session_id,
                stream=stream,
                chat_response_ack=chat_response_ack,
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
) -> Any:
    url = agent_service_ws_url(server, session_id=session_id)
    websocket = await websockets_module.connect(url)
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
                ),
                session_id=session_id,
            ),
            ensure_ascii=False,
        )
    )
    await websocket.recv()
    return websocket


async def _send_chat_and_print_responses(
    websocket: Any,
    *,
    text: str,
    chat_index: str,
    user_number: str,
    session_id: str | None,
    stream: bool,
    chat_response_ack: bool,
) -> bool:
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
            return False
        if message != "chatResponse":
            continue
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
        status = _intent_status(body)
        return status != "FAIL"


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
) -> JsonObject:
    body: JsonObject = {
        "number": user_number,
        "callType": call_type,
        "clientInfo": {
            "clientType": "run_client",
            "clientName": "scripts/run_client.py",
        },
    }
    if model_name:
        body["modelName"] = model_name
    capabilities: JsonObject = {}
    if chat_progress:
        capabilities["chatProgress"] = True
    if chat_response_ack:
        capabilities["chatResponseAck"] = True
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


def print_json(value: JsonObject) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _print_protocol_error(body: JsonObject) -> None:
    message = body.get("message") or body.get("error") or body.get("code") or body
    print(f"ERROR: {message}", file=sys.stderr, flush=True)


def new_session_id() -> str:
    return f"media-client-{uuid.uuid4().hex[:12]}"


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
        "--interactive",
        action="store_true",
        help="Keep the console open after sending the optional first text.",
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
            interactive=args.interactive or args.text is None,
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


def _print_console_help(session_id: str) -> None:
    print(
        "Type text and press Enter to send chat. "
        f"Current session: {session_id}. Commands: /new [sessionId], /session <sessionId>, /help, /quit.",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
