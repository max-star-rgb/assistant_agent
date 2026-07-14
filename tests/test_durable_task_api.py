from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from assistant_agent.api import routes_agent
from assistant_agent.api.app import create_app
from assistant_agent.api.auth import get_auth_context
from assistant_agent.schemas.durable_tasks import TaskCheckpoint
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.services.api_identity import AuthContext
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.tools.registry import create_default_registry


class RuntimeStub:
    def __init__(self, service) -> None:
        self.durable_task_service = service


def test_task_api_is_identity_scoped_and_redacts_internal_bindings(monkeypatch) -> None:
    client, service = _client(monkeypatch, user_id="u1")
    task = _submit(service)

    response = client.get(f"/tasks/{task.task.task_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task"]["task_id"] == task.task.task_id
    serialized = str(payload).lower()
    assert "lease_token" not in serialized
    assert "binding_digest" not in serialized
    assert "idempotency_key" not in serialized

    other_client, _ = _client(monkeypatch, service=service, user_id="u2")
    denied = other_client.get(f"/tasks/{task.task.task_id}")
    assert denied.status_code == 404


def test_task_events_support_cursor_replay(monkeypatch) -> None:
    client, service = _client(monkeypatch)
    task = _submit(service)

    all_events = client.get(f"/tasks/{task.task.task_id}/events").json()
    cursor = all_events["events"][0]["cursor"]
    replay = client.get(
        f"/tasks/{task.task.task_id}/events",
        params={"after": cursor, "limit": 10},
    ).json()

    assert len(all_events["events"]) == 2
    assert all(event["cursor"] > cursor for event in replay["events"])


def test_task_input_and_idempotent_cancel_use_authenticated_identity(monkeypatch) -> None:
    client, service = _client(monkeypatch)
    waiting = _submit(service, requires_followup=True)

    input_response = client.post(
        f"/tasks/{waiting.task.task_id}/input",
        json={"text": "预算 500 元"},
    )
    first_cancel = client.post(
        f"/tasks/{waiting.task.task_id}/cancel",
        json={"reason": "不再需要"},
    )
    second_cancel = client.post(
        f"/tasks/{waiting.task.task_id}/cancel",
        json={"reason": "重复请求"},
    )

    assert input_response.status_code == 200
    assert input_response.json()["task"]["status"] == "queued"
    assert first_cancel.status_code == 200
    assert second_cancel.status_code == 200
    assert second_cancel.json()["task"]["status"] == "cancelled"


def test_task_confirmation_endpoint_resumes_bound_step(monkeypatch) -> None:
    client, service = _client(monkeypatch)
    task = _submit(service)
    lease = service.claim_next(worker_id="worker")
    service.checkpoint(
        lease,
        TaskCheckpoint(
            kind="waiting_confirmation",
            step_id="step_1",
            tool_name="product_search",
            tool_input_digest="digest-1",
            confirmation_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
    )
    waiting = service.store.load(task.task.task_id)

    response = client.post(
        f"/tasks/{task.task.task_id}/confirmations",
        json={
            "confirmation_id": waiting.confirmations[-1].confirmation_id,
            "approved": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "queued"

    invalid = client.post(
        f"/tasks/{task.task.task_id}/confirmations",
        json={"confirmation_id": "confirm-forged", "approved": True},
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "TASK_CONFIRMATION_INVALID"


def test_task_api_returns_stable_disabled_and_validation_errors(monkeypatch) -> None:
    client, _ = _client(monkeypatch, service=None)

    disabled = client.get("/tasks/task_missing")
    enabled_client, _ = _client(monkeypatch)
    invalid_input = enabled_client.post("/tasks/task_missing/input", json={"text": ""})
    forged_identity = enabled_client.post(
        "/tasks/task_missing/cancel",
        json={"reason": "x", "user_id": "forged"},
    )

    assert disabled.status_code == 503
    assert disabled.json()["detail"]["code"] == "DURABLE_TASKS_DISABLED"
    assert invalid_input.status_code == 422
    assert forged_identity.status_code == 422


def _client(monkeypatch, *, service="new", user_id: str = "u1"):
    if service == "new":
        registry = create_default_registry()
        service = DurableTaskService(store=InMemoryTaskStore(), registry=registry)
    monkeypatch.setattr(routes_agent, "_RUNTIME", RuntimeStub(service))
    app = create_app()
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        authenticated=True,
        source="test",
        user_id=user_id,
        session_id="s1",
    )
    return TestClient(app), service


def _submit(service: DurableTaskService, *, requires_followup: bool = False):
    return service.submit_plan(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        ingress_run_id="run-api",
        plan=TaskPlan(
            goal="API task",
            steps=[
                TaskStep(step_id="step_1", action="search", tool_name="product_search")
            ],
            requires_followup=requires_followup,
            followup_question="预算是多少？" if requires_followup else None,
        ),
        revision_reason="initial",
    )
