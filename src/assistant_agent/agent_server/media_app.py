"""Custom Agent Server routes for the existing Media-Agent protocol."""

from __future__ import annotations

from fastapi import FastAPI, WebSocket


app = FastAPI(title="Assistant Agent Server Media Adapter")


@app.get("/health/agent-server-adapter")
async def adapter_health() -> dict[str, str]:
    return {"status": "ok", "execution_owner": "agent_server"}


@app.websocket("/agent-service/{version}")
async def agent_service_websocket(websocket: WebSocket, version: str) -> None:
    """Reserve the compatibility route until its native run adapter is wired."""

    await websocket.accept()
    await websocket.send_json(
        {
            "message": "error",
            "body": '{"code":"adapter_not_ready","recoverable":true}',
        }
    )
    await websocket.close(code=1013, reason=f"agent-service {version} is not ready")


__all__ = ["app"]
