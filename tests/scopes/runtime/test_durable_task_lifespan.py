import time

from fastapi.testclient import TestClient

from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app, get_durable_task_worker
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.services.chat_adapter import ChatResult
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.sqlite_store import SQLiteTaskStore
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.registry import create_default_registry


class TrackingStore(InMemoryTaskStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class RuntimeStub:
    def __init__(self, *, config: ProviderConfig, service=None) -> None:
        self.config = config
        self.durable_task_service = service


class RestartAdapter:
    provider = "scripted-native"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, request):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(
                response_text="",
                tool_calls=[
                    NativeToolCall(
                        id="call-1",
                        name="product_search",
                        arguments={"query": "耳机", "limit": 1},
                    )
                ],
                finish_reason="tool_calls",
                message_kind="tool_call",
                provider=self.provider,
                model="restart-test",
            )
        return ChatResult(
            response_text="任务完成",
            finish_reason="stop",
            message_kind="final_answer",
            provider=self.provider,
            model="restart-test",
        )


def test_lifespan_creates_no_worker_or_store_when_disabled(monkeypatch) -> None:
    runtime = RuntimeStub(config=ProviderConfig(), service=None)
    monkeypatch.setattr(routes_agent, "_RUNTIME", runtime)
    app = create_app()

    with TestClient(app):
        assert app.state.durable_task_service is None
        assert get_durable_task_worker(app) is None

    assert routes_agent._RUNTIME is None


def test_enabled_api_only_store_opens_without_worker_and_closes_once(monkeypatch) -> None:
    store = TrackingStore()
    service = DurableTaskService(store=store, registry=create_default_registry())
    runtime = RuntimeStub(
        config=ProviderConfig(
            durable_tasks_enabled=True,
            durable_task_worker_enabled=False,
        ),
        service=service,
    )
    monkeypatch.setattr(routes_agent, "_RUNTIME", runtime)
    app = create_app()

    with TestClient(app):
        assert app.state.durable_task_service is service
        assert get_durable_task_worker(app) is None
        assert store.closed == 0

    assert store.closed == 1
    assert routes_agent._RUNTIME is None


def test_enabled_worker_starts_once_and_stops_cooperatively(monkeypatch) -> None:
    store = TrackingStore()
    service = DurableTaskService(store=store, registry=create_default_registry())
    runtime = RuntimeStub(
        config=ProviderConfig(
            durable_tasks_enabled=True,
            durable_task_worker_enabled=True,
            durable_task_poll_seconds=0.01,
        ),
        service=service,
    )
    monkeypatch.setattr(routes_agent, "_RUNTIME", runtime)
    app = create_app()

    with TestClient(app):
        worker = get_durable_task_worker(app)
        assert worker is not None
        assert app.state.durable_task_worker_task is not None
        assert app.state.durable_task_stop_event.is_set() is False

    assert app.state.durable_task_stop_event.is_set() is True
    assert app.state.durable_task_worker_task.done() is True
    assert store.closed == 1


def test_repeated_apps_do_not_reuse_closed_runtime_service(monkeypatch) -> None:
    first_store = TrackingStore()
    first_runtime = RuntimeStub(
        config=ProviderConfig(durable_tasks_enabled=True),
        service=DurableTaskService(store=first_store, registry=create_default_registry()),
    )
    monkeypatch.setattr(routes_agent, "_RUNTIME", first_runtime)
    with TestClient(create_app()):
        pass

    second_store = TrackingStore()
    second_runtime = RuntimeStub(
        config=ProviderConfig(durable_tasks_enabled=True),
        service=DurableTaskService(store=second_store, registry=create_default_registry()),
    )
    monkeypatch.setattr(routes_agent, "_RUNTIME", second_runtime)
    with TestClient(create_app()):
        pass

    assert first_store.closed == 1
    assert second_store.closed == 1


def test_queued_sqlite_task_is_claimed_after_app_restart(monkeypatch, tmp_path) -> None:
    path = tmp_path / "durable.sqlite3"
    registry = create_default_registry()
    first_service = DurableTaskService(store=SQLiteTaskStore(path), registry=registry)
    task = first_service.submit_plan(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        ingress_run_id="run-restart",
        plan=TaskPlan(
            goal="restart task",
            steps=[TaskStep(step_id="step_1", action="search", tool_name="product_search")],
        ),
        revision_reason="initial",
    )
    first_runtime = RuntimeStub(
        config=ProviderConfig(
            durable_tasks_enabled=True,
            durable_task_worker_enabled=False,
        ),
        service=first_service,
    )
    monkeypatch.setattr(routes_agent, "_RUNTIME", first_runtime)
    with TestClient(create_app()):
        pass

    second_registry = create_default_registry()
    second_service = DurableTaskService(
        store=SQLiteTaskStore(path),
        registry=second_registry,
    )
    config = ProviderConfig(
        durable_tasks_enabled=True,
        durable_task_worker_enabled=True,
        durable_task_poll_seconds=0.01,
    )
    runtime = AgentGraphRuntime(
        registry=second_registry,
        config=config,
        chat_adapter=RestartAdapter(),
        durable_task_service=second_service,
    )
    monkeypatch.setattr(routes_agent, "_RUNTIME", runtime)
    app = create_app()

    with TestClient(app):
        deadline = time.monotonic() + 3
        status = "queued"
        while time.monotonic() < deadline:
            status = second_service.store.load(task.task.task_id).task.status
            if status == "completed":
                break
            time.sleep(0.01)
        assert status == "completed"
