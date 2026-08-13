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
from assistant_agent.api.routes_agent import router as agent_router
from assistant_agent.api.routes_eval_experiments import (
    router as eval_experiments_router,
)
from assistant_agent.api.routes_tasks import router as tasks_router
from assistant_agent.api.routes_workflows import router as workflows_router
from assistant_agent.api.routes_skills import router as skills_router
from assistant_agent.api.models import PROTOCOL_VERSION, api_error
from assistant_agent.runtime.generated_artifacts import GENERATED_ARTIFACT_DIR
from assistant_agent.runtime.assistant_run_service import resolve_runtime_config
from assistant_agent.runtime.checkpointer import AsyncCheckpointerOwner
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
    NotificationDeliveryWorker,
)
from assistant_agent.api.agent_service_notifications import (
    AgentServiceNotificationTransport,
    get_agent_service_notification_hub,
)
from assistant_agent.runtime.server_startup_summary import prepare_server_startup_report
from assistant_agent.skills.application import (
    create_skill_runtime_app_from_env,
)
from assistant_agent.workflows.worker import DurableWorkflowWorker
from assistant_agent.workflows.graph_host import WorkflowGraphHost
from assistant_agent.workflows.cutover import load_workflow_cutover_manifest
from assistant_agent.workflows.legacy_drain_host import LegacyDrainHost

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
    await start_shared_checkpointer_owner(app)
    try:
        await start_workflow_graph_host(app)
        start_shared_agent_runtime(app)
        await start_durable_task_worker(app)
        await start_durable_workflow_worker(app)
        prepare_server_startup_report(app, app.state.agent_runtime)
        yield
    finally:
        await shutdown_workflow_graph_host(app)
        await shutdown_durable_workflow_worker(app)
        await shutdown_durable_task_worker(app)
        await shutdown_gateway_runtime()
        shutdown_shared_agent_runtime(app)
        await shutdown_shared_checkpointer_owner(app)


async def start_shared_checkpointer_owner(app: FastAPI) -> AsyncCheckpointerOwner:
    """Open the process saver before compiling either production graph."""

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    config = resolve_runtime_config()
    owner = AsyncCheckpointerOwner(config)
    await owner.open()
    app.state.runtime_config = config
    app.state.shared_checkpointer_owner = owner
    return owner


async def start_workflow_graph_host(app: FastAPI) -> WorkflowGraphHost | None:
    """Compile WorkflowGraph on the already-open process saver."""

    config = app.state.runtime_config
    owner = app.state.shared_checkpointer_owner
    app.state.workflow_graph_host = None
    if not config.durable_workflows_enabled:
        return None
    host = await WorkflowGraphHost.open(
        config=config,
        checkpointer_owner=owner,
    )
    app.state.workflow_graph_host = host
    return host


def start_shared_agent_runtime(app: FastAPI) -> Any:
    """Compile Assistant graph on the same saver, then bind Workflow services."""

    owner = app.state.shared_checkpointer_owner
    workflow_host = getattr(app.state, "workflow_graph_host", None)
    runtime, trace_store = routes_agent.create_agent_runtime_for_composition(
        config=app.state.runtime_config,
        checkpointer=owner.checkpointer,
        graph_invocation_claim_store=owner.invocation_claim_store,
        workflow_graph_host=workflow_host,
    )
    if isinstance(workflow_host, WorkflowGraphHost):
        adapter = runtime.chat_adapter
        workflow_host.bind_runtime_services(
            provider_registry={
                "planner": adapter,
                "worker": adapter,
                "verifier": adapter,
            },
            tool_registry=runtime.registry,
        )
    routes_agent.install_agent_runtime(
        runtime,
        owned_trace_store=trace_store,
    )
    app.state.agent_runtime = runtime
    app.state.shared_runtime_lifespan_owned = True
    return runtime


async def shutdown_workflow_graph_host(app: FastAPI) -> None:
    host = getattr(app.state, "workflow_graph_host", None)
    app.state.workflow_graph_host = None
    if isinstance(host, WorkflowGraphHost):
        await host.close()


def shutdown_shared_agent_runtime(app: FastAPI) -> None:
    app.state.shared_runtime_lifespan_owned = False
    routes_agent.shutdown_agent_runtime()
    app.state.agent_runtime = None


async def shutdown_shared_checkpointer_owner(app: FastAPI) -> None:
    owner = getattr(app.state, "shared_checkpointer_owner", None)
    app.state.shared_checkpointer_owner = None
    if isinstance(owner, AsyncCheckpointerOwner):
        await owner.aclose()


def get_durable_task_worker(app: FastAPI) -> DurableTaskWorker | None:
    worker = getattr(app.state, "durable_task_worker", None)
    return worker if isinstance(worker, DurableTaskWorker) else None


def get_durable_workflow_worker(app: FastAPI) -> DurableWorkflowWorker | None:
    worker = getattr(app.state, "durable_workflow_worker", None)
    return worker if isinstance(worker, DurableWorkflowWorker) else None


async def start_durable_workflow_worker(app: FastAPI) -> DurableWorkflowWorker | None:
    """Start the manifest-bounded legacy drain; never derive a dynamic scope."""

    runtime = getattr(app.state, "agent_runtime", None)
    config = getattr(app.state, "runtime_config", None)
    app.state.durable_workflow_worker = None
    if (
        runtime is None
        or config is None
        or not config.durable_workflow_worker_enabled
    ):
        return None
    manifest_path = os.environ.get(
        "MULTIMODAL_AGENT_WORKFLOW_CUTOVER_MANIFEST_PATH", ""
    )
    if not manifest_path:
        raise RuntimeError(
            "durable workflow drain requires an operator cutover manifest"
        )
    host = LegacyDrainHost.compose(
        config=config,
        agent_runtime=runtime,
        manifest=load_workflow_cutover_manifest(manifest_path),
    )
    app.state.legacy_drain_host = host
    app.state.durable_workflow_worker = host.worker
    await host.start()
    return host.worker


async def shutdown_durable_workflow_worker(app: FastAPI) -> None:
    host = getattr(app.state, "legacy_drain_host", None)
    app.state.legacy_drain_host = None
    app.state.durable_workflow_worker = None
    if isinstance(host, LegacyDrainHost):
        await host.close()


async def start_durable_task_worker(app: FastAPI) -> DurableTaskWorker | None:
    """Bind the app to the shared runtime service and optionally start one worker."""

    runtime = routes_agent.get_agent_runtime()
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
        config.durable_notification_worker_enabled
        and notification_store is not None
    ):
        notification_transport = AgentServiceNotificationTransport(
            get_agent_service_notification_hub()
        )
        delivery_worker = NotificationDeliveryWorker(
            store=notification_store,
            transport=notification_transport,
            recipient_availability=notification_transport,
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
    if runtime is not None and not getattr(
        app.state, "shared_runtime_lifespan_owned", False
    ):
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
