"""Runtime WebSocket server entrypoint."""

from __future__ import annotations

import asyncio

from assistant_agent.runtime_gateway.runtime import RuntimeService
from assistant_agent.runtime_gateway.ws import WsEndpoint


async def serve_runtime_ws(*, host: str = "127.0.0.1", port: int = 8766) -> None:
    """Serve the runtime side of the Gateway<->Runtime WebSocket stream."""

    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - optional runtime dependency.
        raise RuntimeError("Install websockets to use assistant_agent.runtime_gateway.ws_server") from exc

    runtime = RuntimeService()

    async def handler(ws) -> None:
        ep = WsEndpoint.wrap(ws)
        await runtime.serve(ep)  # type: ignore[arg-type]

    async with websockets.serve(handler, host, port):
        await asyncio.Future()


def main() -> None:
    asyncio.run(serve_runtime_ws())


if __name__ == "__main__":
    main()
