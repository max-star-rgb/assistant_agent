"""FastAPI application factory."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from assistant_agent.api.agent_service_websocket import router as agent_service_websocket_router
from assistant_agent.api.gateway_runtime import shutdown_gateway_runtime
from assistant_agent.api.gateway_websocket import router as gateway_websocket_router
from assistant_agent.api.rendering_3d_callback import router as rendering_3d_callback_router
from assistant_agent.api import routes_agent
from assistant_agent.api.routes_a2a import router as a2a_router
from assistant_agent.api.routes_agent import router as agent_router, shutdown_agent_runtime
from assistant_agent.api.routes_eval_experiments import (
    router as eval_experiments_router,
)
from assistant_agent.api.routes_tasks import router as tasks_router
from assistant_agent.api.routes_workflows import router as workflows_router
from assistant_agent.api.routes_skills import router as skills_router
from assistant_agent.api.models import PROTOCOL_VERSION, api_error
from assistant_agent.runtime.generated_artifacts import GENERATED_ARTIFACT_DIR
from assistant_agent.automation.durable_tasks.hotel_price_watch import (
    HOTEL_PRICE_WATCH_PROFILE,
    HotelPriceWatchRuntime,
)
from assistant_agent.automation.durable_tasks.worker import (
    DurableTaskRuntimeRouter,
    DurableTaskWorker,
)
from assistant_agent.observability.operational_logging import configure_operational_logging_from_env
from assistant_agent.automation.proactive_wake.delivery import (
    MockProactiveNotificationTransport,
    NotificationDeliveryWorker,
)
from assistant_agent.runtime.server_startup_summary import print_tool_registry_summary
from assistant_agent.skills.application import (
    create_skill_runtime_app_from_env,
)
from assistant_agent.workflows.context import WorkflowContextCompiler
from assistant_agent.workflows.execution import AgentRuntimeWorkItemExecutor
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.worker import DurableWorkflowWorker

SKIP_DOTENV_ENV = "MULTIMODAL_AGENT_SKIP_DOTENV"


def create_app() -> FastAPI:
    load_repo_env_file()
    configure_operational_logging_from_env()
    app = FastAPI(title="Multimodal Agent", lifespan=_lifespan)
    app.state.skill_app = create_skill_runtime_app_from_env()

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

    GENERATED_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/artifacts/generated", StaticFiles(directory=GENERATED_ARTIFACT_DIR), name="generated_artifacts")
    app.include_router(agent_router)
    app.include_router(eval_experiments_router)
    app.include_router(tasks_router)
    app.include_router(workflows_router)
    app.include_router(skills_router)
    app.include_router(a2a_router)
    app.include_router(agent_service_websocket_router)
    app.include_router(gateway_websocket_router)
    app.include_router(rendering_3d_callback_router)
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await start_durable_task_worker(app)
    await start_durable_workflow_worker(app)
    try:
        yield
    finally:
        await shutdown_durable_workflow_worker(app)
        await shutdown_durable_task_worker(app)
        await shutdown_gateway_runtime()
        shutdown_agent_runtime()


def get_durable_task_worker(app: FastAPI) -> DurableTaskWorker | None:
    worker = getattr(app.state, "durable_task_worker", None)
    return worker if isinstance(worker, DurableTaskWorker) else None


def get_durable_workflow_worker(app: FastAPI) -> DurableWorkflowWorker | None:
    worker = getattr(app.state, "durable_workflow_worker", None)
    return worker if isinstance(worker, DurableWorkflowWorker) else None


async def start_durable_workflow_worker(app: FastAPI) -> DurableWorkflowWorker | None:
    runtime = getattr(app.state, "agent_runtime", None) or routes_agent.get_agent_runtime()
    service = getattr(runtime, "workflow_service", None)
    artifact_store = getattr(runtime, "workflow_artifact_store", None)
    config = getattr(runtime, "config", None)
    app.state.durable_workflow_worker = None
    app.state.durable_workflow_stop_event = None
    app.state.durable_workflow_worker_task = None
    app.state.durable_workflow_store_closed = False
    app.state.durable_workflow_artifact_store_closed = False
    if (
        service is None
        or artifact_store is None
        or config is None
        or not config.durable_workflow_worker_enabled
    ):
        return None
    stop_event = Event()
    work_item_executor = AgentRuntimeWorkItemExecutor(
        agent_runtime=runtime,
        artifact_store=artifact_store,
        context_compiler=WorkflowContextCompiler(artifact_store=artifact_store),
        max_iterations=config.max_tool_iterations,
    )
    worker = DurableWorkflowWorker(
        service=service,
        runtime=WorkflowRuntime(
            service=service,
            work_item_executor=work_item_executor,
        ),
        worker_id=f"api-workflow-worker-{os.getpid()}-{id(app)}",
        lease_seconds=config.durable_workflow_lease_seconds,
        poll_seconds=config.durable_workflow_poll_seconds,
    )
    app.state.durable_workflow_worker = worker
    app.state.durable_workflow_stop_event = stop_event
    app.state.durable_workflow_worker_task = asyncio.create_task(
        asyncio.to_thread(worker.run, stop_event)
    )
    return worker


async def shutdown_durable_workflow_worker(app: FastAPI) -> None:
    stop_event = getattr(app.state, "durable_workflow_stop_event", None)
    worker_task = getattr(app.state, "durable_workflow_worker_task", None)
    if stop_event is not None:
        stop_event.set()
    if worker_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(worker_task), timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
    runtime = getattr(app.state, "agent_runtime", None)
    service = getattr(runtime, "workflow_service", None) if runtime is not None else None
    artifacts = (
        getattr(runtime, "workflow_artifact_store", None)
        if runtime is not None
        else None
    )
    if service is not None and not app.state.durable_workflow_store_closed:
        service.store.close()
        app.state.durable_workflow_store_closed = True
    if artifacts is not None and not app.state.durable_workflow_artifact_store_closed:
        artifacts.close()
        app.state.durable_workflow_artifact_store_closed = True


async def start_durable_task_worker(app: FastAPI) -> DurableTaskWorker | None:
    """Bind the app to the shared runtime service and optionally start one worker."""

    runtime = routes_agent.get_agent_runtime()
    print_tool_registry_summary(runtime.registry)
    service = getattr(runtime, "durable_task_service", None)
    config = getattr(runtime, "config", None)
    app.state.agent_runtime = runtime
    app.state.durable_task_service = service
    app.state.durable_task_worker = None
    app.state.durable_task_stop_event = None
    app.state.durable_task_worker_task = None
    app.state.notification_delivery_worker = None
    app.state.notification_delivery_worker_task = None
    app.state.durable_task_store_closed = False
    if service is None or config is None or not config.durable_task_worker_enabled:
        return None
    stop_event = Event()
    worker_runtime: Any = runtime
    if "lodging_search" in runtime.registry.list():
        worker_runtime = DurableTaskRuntimeRouter(
            default_runtime=runtime,
            profile_runtimes={
                HOTEL_PRICE_WATCH_PROFILE: HotelPriceWatchRuntime(
                    task_service=service,
                    registry=runtime.registry,
                )
            },
        )
    worker = DurableTaskWorker(
        service=service,
        runtime=worker_runtime,
        worker_id=f"api-worker-{os.getpid()}-{id(app)}",
        poll_seconds=config.durable_task_poll_seconds,
    )
    app.state.durable_task_worker = worker
    app.state.durable_task_stop_event = stop_event
    app.state.durable_task_worker_task = asyncio.create_task(
        asyncio.to_thread(worker.run, stop_event)
    )
    notification_store = getattr(runtime, "notification_outbox_store", None)
    if (
        config.provider_mode == "mock"
        and config.durable_notification_worker_enabled
        and notification_store is not None
    ):
        delivery_worker = NotificationDeliveryWorker(
            store=notification_store,
            transport=MockProactiveNotificationTransport(),
            delivery_observer=service,
        )
        app.state.notification_delivery_worker = delivery_worker
        app.state.notification_delivery_worker_task = asyncio.create_task(
            _run_notification_delivery_worker(
                delivery_worker,
                stop_event=stop_event,
                poll_seconds=config.durable_task_poll_seconds,
            )
        )
    return worker


async def shutdown_durable_task_worker(app: FastAPI) -> None:
    """Stop the cooperative worker, close its runtime-owned store, and release runtime."""

    stop_event = getattr(app.state, "durable_task_stop_event", None)
    worker_task = getattr(app.state, "durable_task_worker_task", None)
    delivery_task = getattr(
        app.state,
        "notification_delivery_worker_task",
        None,
    )
    if stop_event is not None:
        stop_event.set()
    if worker_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(worker_task), timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
    if delivery_task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(delivery_task), timeout=5.0)
        except asyncio.TimeoutError:
            delivery_task.cancel()
    service = getattr(app.state, "durable_task_service", None)
    if service is not None and not getattr(app.state, "durable_task_store_closed", False):
        close = getattr(service.store, "close", None)
        if callable(close):
            close()
        app.state.durable_task_store_closed = True
    runtime: Any = getattr(app.state, "agent_runtime", None)
    if runtime is not None:
        routes_agent.release_agent_runtime(runtime)


async def _run_notification_delivery_worker(
    worker: NotificationDeliveryWorker,
    *,
    stop_event: Event,
    poll_seconds: float,
) -> None:
    while not stop_event.is_set():
        await worker.drain_once()
        await asyncio.to_thread(stop_event.wait, poll_seconds)


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
