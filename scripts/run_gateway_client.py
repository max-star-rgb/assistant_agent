"""Local Gateway WebSocket smoke client."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.gateway import CALL_INCOMING, frame  # noqa: E402
from assistant_agent.gateway.ws import dumps_frame, loads_frame  # noqa: E402


async def run_gateway_smoke(
    *,
    server: str,
    user_id: str,
    session_id: str,
    text: str,
    video_id: str | None = None,
) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional operator dependency.
        raise RuntimeError("Install websockets to use scripts/run_gateway_client.py") from exc

    url = _gateway_ws_url(server, user_id=user_id, session_id=session_id)
    async with websockets.connect(url) as websocket:
        await websocket.send(
            dumps_frame(
                frame(
                    type=CALL_INCOMING,
                    user_id=user_id,
                    session_id=session_id,
                    payload={"config": {"client": "run_gateway_client"}},
                )
            )
        )
        ready = loads_frame(await websocket.recv())
        print(json.dumps(ready, ensure_ascii=False))

        payload: dict[str, Any] = {"text": text}
        if video_id:
            payload["video_ids"] = [video_id]
        await websocket.send(
            dumps_frame(
                frame(
                    type="message.user",
                    user_id=user_id,
                    session_id=session_id,
                    payload=payload,
                )
            )
        )

        async for raw in websocket:
            received = loads_frame(raw)
            print(json.dumps(received, ensure_ascii=False))
            if received.get("type") == "run.end":
                return 0 if received.get("reason") == "completed" else 1
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local Gateway WebSocket smoke request.")
    parser.add_argument("text", help="User text to send as a Gateway message.user frame.")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="HTTP server base URL.")
    parser.add_argument("--user-id", default="gateway_smoke_user")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--video-id", default=None, help="Optional video reference to send with the message.")
    args = parser.parse_args()

    session_id = args.session_id or f"gateway-smoke-{uuid.uuid4()}"
    raise SystemExit(
        asyncio.run(
            run_gateway_smoke(
                server=args.server,
                user_id=args.user_id,
                session_id=session_id,
                text=args.text,
                video_id=args.video_id,
            )
        )
    )


def _gateway_ws_url(server: str, *, user_id: str, session_id: str) -> str:
    base = server.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    query = urlencode({"user_id": user_id, "session_id": session_id, "client": "cli"})
    return f"{base}/ws/gateway?{query}"


if __name__ == "__main__":
    main()
