"""FastAPI application factory."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from multimodal_agent.api.routes_agent import router as agent_router
from multimodal_agent.api.websocket import router as websocket_router
from multimodal_agent.schemas.api import PROTOCOL_VERSION, api_error
from multimodal_agent.services.generated_artifacts import GENERATED_ARTIFACT_DIR

SKIP_DOTENV_ENV = "MULTIMODAL_AGENT_SKIP_DOTENV"


def create_app() -> FastAPI:
    load_repo_env_file()
    app = FastAPI(title="Multimodal Agent")
    static_dir = Path(__file__).resolve().parent / "static"

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

    @app.get("/demo/console", include_in_schema=False)
    def demo_console() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    GENERATED_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts/generated", StaticFiles(directory=GENERATED_ARTIFACT_DIR), name="generated_artifacts")
    app.include_router(agent_router)
    app.include_router(websocket_router)
    return app


def load_repo_env_file(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load repo `.env` for manual API/Web runs without adding a dependency."""

    if (
        "PYTEST_CURRENT_TEST" in os.environ
        or os.environ.get("MULTIMODAL_AGENT_DISABLE_DOTENV") == "1"
        or os.environ.get(SKIP_DOTENV_ENV) == "1"
    ):
        return {}
    env_path = path or Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if not key:
            continue
        loaded[key] = _strip_env_value(value.strip())
        if override or key not in os.environ:
            os.environ[key] = loaded[key]
    return loaded


def _strip_env_value(value: str) -> str:
    quote_pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}
    comment_index = value.find(" #")
    if comment_index >= 0:
        value = value[:comment_index].strip()
    if len(value) >= 2 and (value[0], value[-1]) in quote_pairs:
        value = value[1:-1]
    return value.strip().strip('"').strip("'").strip("“”‘’")


def _validation_error_summary(item: dict) -> dict[str, str]:
    return {
        "loc": ".".join(str(part) for part in item.get("loc", [])),
        "message": str(item.get("msg", "Invalid value")),
        "type": str(item.get("type", "value_error")),
    }
