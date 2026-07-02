"""Gateway session WebSocket server entrypoint."""

from __future__ import annotations

import asyncio

from assistant_agent.gateway.session import GatewaySessionService
from assistant_agent.gateway.ws import WsEndpoint


async def serve_gateway_session_ws(*, host: str = "127.0.0.1", port: int = 8766) -> None:
    """Serve the session side of the Gateway<->agent WebSocket stream."""

    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional runtime dependency.
        raise RuntimeError("Install websockets to use assistant_agent.gateway.ws_server") from exc

    session = GatewaySessionService()

    async def handler(ws) -> None:
        ep = WsEndpoint.wrap(ws)
        await session.serve(ep)  # type: ignore[arg-type]

    async with websockets.serve(handler, host, port):
        await asyncio.Future()


def main() -> None:
    asyncio.run(serve_gateway_session_ws())

if __name__ == "__main__":
    main()
