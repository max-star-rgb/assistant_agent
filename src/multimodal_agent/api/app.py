"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from multimodal_agent.api.routes_agent import router as agent_router
from multimodal_agent.api.websocket import router as websocket_router
from multimodal_agent.schemas.api import PROTOCOL_VERSION, api_error


def create_app() -> FastAPI:
    app = FastAPI(title="Multimodal Agent")

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
        error = api_error(
            "INVALID_REQUEST",
            "请求参数无效。",
            detail={"fields": [_validation_error_summary(item) for item in exc.errors()]},
            recoverable=True,
        )
        return JSONResponse(
            status_code=422,
            content={
                "protocol_version": PROTOCOL_VERSION,
                "status": "error",
                "errors": [error.model_dump(mode="json")],
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(agent_router)
    app.include_router(websocket_router)
    return app


def _validation_error_summary(item: dict) -> dict[str, str]:
    return {
        "loc": ".".join(str(part) for part in item.get("loc", [])),
        "message": str(item.get("msg", "Invalid value")),
        "type": str(item.get("type", "value_error")),
    }


app = create_app()
