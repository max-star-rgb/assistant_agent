"""Read-only local control-plane observability helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from assistant_agent.schemas.agent_control_plane import (
    AgentAuditEvent,
    AgentAuditEventList,
    AgentControlPlaneBudgetSummary,
    AgentControlPlaneDelegationNode,
    AgentControlPlaneDelegationTree,
    AgentControlPlaneReplayPreview,
    AgentControlPlaneRouteSummary,
    AgentControlPlaneRunRecord,
    AgentControlPlaneRunSummary,
)
from assistant_agent.schemas.agent_router import AgentRouteRequest
from assistant_agent.schemas.api import AgentRunResponse
from assistant_agent.services.provider_errors import sanitize_error_detail, sanitize_error_message
from assistant_agent.services.trace_query import RunSummary, TraceQueryService, TraceSummary


CONTROL_PLANE_REDACTION = {
    "raw_payloads_included": False,
    "auth_tokens_included": False,
    "provider_raw_responses_included": False,
    "media_bodies_included": False,
    "conversation_history_included": False,
}
AUDIT_RETENTION = {
    "storage": "process_local_memory",
    "durable": False,
    "retention_policy": "process_lifetime_only",
    "phase": "phase_d_first_pass",
}


class AgentControlPlaneStore(Protocol):
    """Storage boundary for local control-plane run records."""

    def record(self, record: AgentControlPlaneRunRecord) -> None:
        """Store or replace one run record."""

    def get(self, run_id: str) -> AgentControlPlaneRunRecord | None:
        """Return one stored run record."""

    def get_by_trace_id(self, trace_id: str) -> AgentControlPlaneRunRecord | None:
        """Return one stored run record by trace id."""

    def append_audit_event(self, event: AgentAuditEvent) -> None:
        """Store one redacted audit event."""

    def list_audit_events(
        self,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AgentAuditEvent]:
        """Return audit events matching filters."""


class InMemoryAgentControlPlaneStore:
    """Process-local control-plane record store for local/pilot operation."""

    def __init__(self) -> None:
        self._records_by_run_id: dict[str, AgentControlPlaneRunRecord] = {}
        self._run_id_by_trace_id: dict[str, str] = {}
        self._audit_events: list[AgentAuditEvent] = []

    def record(self, record: AgentControlPlaneRunRecord) -> None:
        self._records_by_run_id[record.run_id] = record
        if record.trace_id:
            self._run_id_by_trace_id[record.trace_id] = record.run_id

    def get(self, run_id: str) -> AgentControlPlaneRunRecord | None:
        return self._records_by_run_id.get(run_id)

    def get_by_trace_id(self, trace_id: str) -> AgentControlPlaneRunRecord | None:
        run_id = self._run_id_by_trace_id.get(trace_id)
        return self.get(run_id) if run_id else None

    def append_audit_event(self, event: AgentAuditEvent) -> None:
        self._audit_events.append(event)

    def list_audit_events(
        self,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AgentAuditEvent]:
        events = [
            event
            for event in self._audit_events
            if (run_id is None or event.run_id == run_id)
            and (trace_id is None or event.trace_id == trace_id)
            and (user_id is None or event.user_id == user_id)
            and (session_id is None or event.session_id == session_id)
            and (event_type is None or event.event_type == event_type)
        ]
        return events[-max(limit, 0) :]

    def retention(self) -> dict[str, Any]:
        return dict(AUDIT_RETENTION)


class JsonlAgentControlPlaneStore:
    """JSONL-backed control-plane store for explicit local durable traces."""

    def __init__(self, path: Path | str = ".data/agent_control_plane.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: AgentControlPlaneRunRecord) -> None:
        self._append("run_record", record.model_dump(mode="json"))

    def get(self, run_id: str) -> AgentControlPlaneRunRecord | None:
        records = [record for record in self._read_records() if record.run_id == run_id]
        return records[-1] if records else None

    def get_by_trace_id(self, trace_id: str) -> AgentControlPlaneRunRecord | None:
        records = [record for record in self._read_records() if record.trace_id == trace_id]
        return records[-1] if records else None

    def append_audit_event(self, event: AgentAuditEvent) -> None:
        self._append("audit_event", event.model_dump(mode="json"))

    def list_audit_events(
        self,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AgentAuditEvent]:
        events = [
            event
            for event in self._read_audit_events()
            if (run_id is None or event.run_id == run_id)
            and (trace_id is None or event.trace_id == trace_id)
            and (user_id is None or event.user_id == user_id)
            and (session_id is None or event.session_id == session_id)
            and (event_type is None or event.event_type == event_type)
        ]
        return events[-max(limit, 0) :]

    def retention(self) -> dict[str, Any]:
        return {
            "storage": "jsonl_file",
            "durable": True,
            "retention_policy": "explicit_local_file_until_deleted",
            "phase": "phase4_multi_agent_readiness",
        }

    def _append(self, kind: str, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False) + "\n")

    def _read_records(self) -> list[AgentControlPlaneRunRecord]:
        records: list[AgentControlPlaneRunRecord] = []
        for envelope in self._read_envelopes():
            if envelope.get("kind") == "run_record" and isinstance(envelope.get("payload"), dict):
                records.append(AgentControlPlaneRunRecord.model_validate(envelope["payload"]))
        return records

    def _read_audit_events(self) -> list[AgentAuditEvent]:
        events: list[AgentAuditEvent] = []
        for envelope in self._read_envelopes():
            if envelope.get("kind") == "audit_event" and isinstance(envelope.get("payload"), dict):
                events.append(AgentAuditEvent.model_validate(envelope["payload"]))
        return events

    def _read_envelopes(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        envelopes: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                if isinstance(payload, dict):
                    envelopes.append(payload)
        return envelopes


class AgentControlPlaneQueryService:
    """Compose run, trace, router, delegation, and budget summaries."""

    def __init__(
        self,
        *,
        trace_query: TraceQueryService,
        router_store: AgentControlPlaneStore | None = None,
    ) -> None:
        self.trace_query = trace_query
        self.router_store = router_store

    def run_summary(self, run_id: str) -> AgentControlPlaneRunSummary | None:
        record = self.router_store.get(run_id) if self.router_store is not None else None
        trace = self.trace_query.run_summary(run_id)
        if record is None and trace is None:
            return None
        return _run_summary(record=record, trace=trace)

    def trace_summary(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.trace_query.trace_summary(trace_id)
        record = self.router_store.get_by_trace_id(trace_id) if self.router_store is not None else None
        if trace is None and record is None:
            return None
        return {
            "schema_version": "agent_control_plane_trace_v1",
            "trace_id": trace_id,
            "run_id": trace.run_id if trace is not None else record.run_id if record is not None else None,
            "agent_router": _record_payload(record),
            "trace": trace.model_dump(mode="json") if trace is not None else {},
            "redaction": CONTROL_PLANE_REDACTION,
        }

    def route_summary(self, run_id: str) -> AgentControlPlaneRouteSummary | None:
        record = self.router_store.get(run_id) if self.router_store is not None else None
        if record is None:
            return None
        return AgentControlPlaneRouteSummary(
            run_id=record.run_id,
            trace_id=record.trace_id,
            route_decision=dict(record.route_decision),
            route_status=_string_or_none(record.route_decision.get("status")),
            failure_class=record.failure_class,
            redaction=CONTROL_PLANE_REDACTION,
        )

    def delegation_tree(self, run_id: str) -> AgentControlPlaneDelegationTree | None:
        record = self.router_store.get(run_id) if self.router_store is not None else None
        if record is None:
            return None
        route_agent_id = _string_or_none(record.route_decision.get("selected_agent_id"))
        return AgentControlPlaneDelegationTree(
            parent_run_id=record.run_id,
            parent_trace_id=record.trace_id,
            root=AgentControlPlaneDelegationNode(
                run_id=record.run_id,
                trace_id=record.trace_id,
                agent_id=route_agent_id,
                status=record.status,
            ),
            children=[_delegation_node(task) for task in record.delegated_tasks],
            redaction=CONTROL_PLANE_REDACTION,
        )

    def budget_summary(self, run_id: str) -> AgentControlPlaneBudgetSummary | None:
        record = self.router_store.get(run_id) if self.router_store is not None else None
        trace = self.trace_query.run_summary(run_id)
        if record is None and trace is None:
            return None
        budget = dict(record.budget) if record is not None else {}
        if trace is not None:
            budget.setdefault("trace_context_budget", _trace_budget(trace))
            budget.setdefault("budget_exceeded", trace.budget_exceeded)
            budget.setdefault("retry_count", trace.retry_count)
        return AgentControlPlaneBudgetSummary(
            run_id=run_id,
            trace_id=(record.trace_id if record is not None else trace.trace_id if trace is not None else None),
            budget=sanitize_error_detail(budget),
            cost=dict(record.cost) if record is not None else {},
            latency_ms=record.latency_ms if record is not None else None,
            redaction=CONTROL_PLANE_REDACTION,
        )

    def replay_preview(self, run_id: str) -> AgentControlPlaneReplayPreview | None:
        record = self.router_store.get(run_id) if self.router_store is not None else None
        if record is None:
            return None
        return AgentControlPlaneReplayPreview(
            run_id=record.run_id,
            trace_id=record.trace_id,
            request={
                "entrypoint": record.entrypoint,
                "user_id": record.user_id,
                "session_id": record.session_id,
                "identity": dict(record.identity),
                "message": "not_included",
            },
            route_decision=dict(record.route_decision),
            delegated_tasks=list(record.delegated_tasks),
            failure_class=record.failure_class,
            replay_notes=[
                "Replay preview is redacted and diagnostic only.",
                "Raw user text, auth tokens, provider payloads, and media bodies are not included.",
                "Use explicit local/pilot configuration before replaying any real provider or remote-agent path.",
            ],
            redaction=CONTROL_PLANE_REDACTION,
        )

    def audit_events(
        self,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> AgentAuditEventList:
        events = (
            self.router_store.list_audit_events(
                run_id=run_id,
                trace_id=trace_id,
                user_id=user_id,
                session_id=session_id,
                event_type=event_type,
                limit=limit,
            )
            if self.router_store is not None
            else []
        )
        return AgentAuditEventList(
            total=len(events),
            events=events,
            retention=_audit_retention(self.router_store),
            redaction=CONTROL_PLANE_REDACTION,
        )

    def audit_events_by_run(self, run_id: str, *, limit: int = 100) -> AgentAuditEventList:
        return self.audit_events(run_id=run_id, limit=limit)


def build_agent_router_run_record(
    *,
    request: AgentRouteRequest,
    response: AgentRunResponse,
    latency_ms: int | None,
) -> AgentControlPlaneRunRecord:
    """Build a redacted control-plane record from one AgentRouter response."""

    router = _router_payload(response)
    route_decision = _dict_or_empty(router.get("route_decision"))
    delegated_tasks = _delegated_tasks(response=response, router=router)
    identity = _identity_payload(request.metadata.get("request_identity"))
    budget = _budget_payload(response=response, delegated_tasks=delegated_tasks)
    cost = _cost_payload(budget)
    errors = [error.model_dump(mode="json") for error in response.errors]
    return AgentControlPlaneRunRecord(
        run_id=response.run_id,
        trace_id=response.trace_id,
        user_id=response_user_id(request, identity),
        session_id=request.session_id,
        status=response.status,
        route_decision=sanitize_error_detail(route_decision),
        delegated_tasks=sanitize_error_detail(delegated_tasks),
        identity=identity,
        budget=budget,
        cost=cost,
        latency_ms=latency_ms,
        error_count=len(errors),
        errors=sanitize_error_detail(errors),
        failure_class=_failure_class(response=response, route_decision=route_decision, delegated_tasks=delegated_tasks),
        redaction=CONTROL_PLANE_REDACTION,
    )


def audit_event(
    *,
    event_type: str,
    component: str,
    action: str,
    outcome: str,
    user_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
    correlation_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AgentAuditEvent:
    """Build one redacted audit event."""

    return AgentAuditEvent(
        event_type=event_type,
        component=component,
        action=action,
        outcome=outcome,
        user_id=sanitize_error_message(user_id) if user_id else None,
        session_id=sanitize_error_message(session_id) if session_id else None,
        run_id=sanitize_error_message(run_id) if run_id else None,
        trace_id=sanitize_error_message(trace_id) if trace_id else None,
        correlation_id=sanitize_error_message(correlation_id) if correlation_id else None,
        detail=sanitize_error_detail(detail or {}),
        redaction=CONTROL_PLANE_REDACTION,
    )


def audit_events_from_agent_router_record(record: AgentControlPlaneRunRecord) -> list[AgentAuditEvent]:
    """Build structured audit events from one AgentRouter run record."""

    events = [
        audit_event(
            event_type="auth_decision",
            component="api_identity",
            action="resolve_request_identity",
            outcome=_auth_outcome(record.identity),
            user_id=record.user_id,
            session_id=record.session_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            detail={
                "identity_source": record.identity.get("identity_source"),
                "auth_bound_identity": record.identity.get("auth_bound_identity"),
                "auth_context_source": record.identity.get("auth_context_source"),
                "warnings": record.identity.get("warnings", []),
            },
        ),
        audit_event(
            event_type="route_decision",
            component="agent_router",
            action="route_request",
            outcome="allowed" if record.route_decision.get("status") == "routed" else "blocked",
            user_id=record.user_id,
            session_id=record.session_id,
            run_id=record.run_id,
            trace_id=record.trace_id,
            detail={
                "selected_agent_id": record.route_decision.get("selected_agent_id"),
                "requested_target_agent_id": record.route_decision.get("requested_target_agent_id"),
                "requested_capability": record.route_decision.get("requested_capability"),
                "collaboration_mode": record.route_decision.get("collaboration_mode"),
                "reason": record.route_decision.get("reason"),
                "status": record.route_decision.get("status"),
                "error_code": record.route_decision.get("error_code"),
            },
        ),
        _provider_opt_in_event(record),
    ]
    for task in record.delegated_tasks:
        events.append(_delegation_event(record, task))
        remote_event = _remote_a2a_event(record, task)
        if remote_event is not None:
            events.append(remote_event)
    return events


def _audit_retention(router_store: AgentControlPlaneStore | None) -> dict[str, Any]:
    retention = getattr(router_store, "retention", None)
    if callable(retention):
        value = retention()
        if isinstance(value, dict):
            return dict(value)
    return dict(AUDIT_RETENTION)


def response_user_id(request: AgentRouteRequest, identity: dict[str, Any]) -> str:
    requested = identity.get("requested_user_id")
    return str(requested) if isinstance(requested, str) and requested else request.user_id


def _auth_outcome(identity: dict[str, Any]) -> str:
    if identity.get("auth_bound_identity") is True:
        return "allowed"
    warnings = identity.get("warnings")
    if isinstance(warnings, list) and warnings:
        return "warning"
    return "local_request_identity"


def _provider_opt_in_event(record: AgentControlPlaneRunRecord) -> AgentAuditEvent:
    provider_budget = record.budget.get("provider_budget")
    allow_real_provider = provider_budget.get("allow_real_provider") if isinstance(provider_budget, dict) else None
    return audit_event(
        event_type="provider_opt_in_decision",
        component="provider_policy",
        action="evaluate_provider_budget",
        outcome="allowed" if allow_real_provider is True else "blocked_default",
        user_id=record.user_id,
        session_id=record.session_id,
        run_id=record.run_id,
        trace_id=record.trace_id,
        detail={
            "allow_real_provider": allow_real_provider,
            "provider_call_count": provider_budget.get("provider_call_count") if isinstance(provider_budget, dict) else None,
            "cost_unit": provider_budget.get("cost_unit") if isinstance(provider_budget, dict) else None,
        },
    )


def _delegation_event(record: AgentControlPlaneRunRecord, task: dict[str, Any]) -> AgentAuditEvent:
    status = _string_or_none(task.get("status"))
    return audit_event(
        event_type="delegation_decision",
        component="agent_delegation",
        action="delegate_to_agent",
        outcome="allowed" if status and status != "failed" else "blocked",
        user_id=record.user_id,
        session_id=record.session_id,
        run_id=record.run_id,
        trace_id=record.trace_id,
        correlation_id=_correlation_id_from_task(task),
        detail={
            "task_id": task.get("task_id"),
            "target_agent_id": task.get("target_agent_id"),
            "child_run_id": task.get("run_id"),
            "child_trace_id": task.get("trace_id"),
            "status": status,
            "artifact_count": task.get("artifact_count"),
            "error_codes": task.get("error_codes", []),
        },
    )


def _remote_a2a_event(record: AgentControlPlaneRunRecord, task: dict[str, Any]) -> AgentAuditEvent | None:
    error_codes = {str(code) for code in task.get("error_codes", []) if code}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    if metadata.get("transport") != "a2a_json_rpc" and not any(code.startswith("agent_remote_") for code in error_codes):
        return None
    return audit_event(
        event_type="remote_a2a_decision",
        component="a2a_json_rpc_transport",
        action="send_remote_agent_task",
        outcome="blocked" if error_codes else "allowed",
        user_id=record.user_id,
        session_id=record.session_id,
        run_id=record.run_id,
        trace_id=record.trace_id,
        correlation_id=_correlation_id_from_task(task),
        detail={
            "target_agent_id": task.get("target_agent_id"),
            "transport": metadata.get("transport"),
            "endpoint_host": metadata.get("endpoint_host"),
            "remote_status_state": metadata.get("remote_status_state"),
            "error_codes": sorted(error_codes),
        },
    )


def _correlation_id_from_task(task: dict[str, Any]) -> str | None:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    value = metadata.get("correlation_id")
    return str(value) if isinstance(value, str) and value else None


def _run_summary(
    *,
    record: AgentControlPlaneRunRecord | None,
    trace: RunSummary | None,
) -> AgentControlPlaneRunSummary:
    if record is not None:
        trace_payload = trace.model_dump(mode="json") if trace is not None else {}
        return AgentControlPlaneRunSummary(
            run_id=record.run_id,
            trace_id=record.trace_id,
            user_id=record.user_id,
            session_id=record.session_id,
            status=record.status,
            source="agent_router",
            route_decision=dict(record.route_decision),
            delegated_tasks=list(record.delegated_tasks),
            identity=dict(record.identity),
            budget=dict(record.budget),
            cost=dict(record.cost),
            latency_ms=record.latency_ms,
            failure_class=record.failure_class,
            error_count=record.error_count,
            errors=list(record.errors),
            trace=trace_payload,
            redaction=CONTROL_PLANE_REDACTION,
        )
    assert trace is not None
    return AgentControlPlaneRunSummary(
        run_id=trace.run_id,
        trace_id=trace.trace_id,
        user_id=trace.user_id,
        session_id=trace.session_id,
        status="failed" if trace.error_count else "completed",
        source="trace_store",
        budget={
            "trace_context_budget": _trace_budget(trace),
            "budget_exceeded": trace.budget_exceeded,
            "retry_count": trace.retry_count,
        },
        error_count=trace.error_count,
        trace=trace.model_dump(mode="json"),
        redaction=CONTROL_PLANE_REDACTION,
    )


def _record_payload(record: AgentControlPlaneRunRecord | None) -> dict[str, Any]:
    return record.model_dump(mode="json") if record is not None else {}


def _delegation_node(task: dict[str, Any]) -> AgentControlPlaneDelegationNode:
    return AgentControlPlaneDelegationNode(
        run_id=_string_or_none(task.get("run_id")),
        trace_id=_string_or_none(task.get("trace_id")),
        task_id=_string_or_none(task.get("task_id")),
        agent_id=_string_or_none(task.get("target_agent_id")),
        status=_string_or_none(task.get("status")),
        artifact_count=_int_or_zero(task.get("artifact_count")),
        error_codes=[str(code) for code in task.get("error_codes", []) if code],
    )


def _router_payload(response: AgentRunResponse) -> dict[str, Any]:
    data = response.data.get("agent_router") if isinstance(response.data, dict) else None
    if isinstance(data, dict):
        return data
    runtime = response.runtime_info.get("agent_router") if isinstance(response.runtime_info, dict) else None
    return dict(runtime) if isinstance(runtime, dict) else {}


def _delegated_tasks(
    *,
    response: AgentRunResponse,
    router: dict[str, Any],
) -> list[dict[str, Any]]:
    metadata_by_task_id = _delegated_task_metadata_by_id(response)
    tasks = router.get("delegated_tasks")
    if isinstance(tasks, list) and tasks:
        merged: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            payload = dict(task)
            task_id = payload.get("task_id")
            if isinstance(task_id, str) and task_id in metadata_by_task_id:
                payload["metadata"] = _safe_delegation_metadata(metadata_by_task_id[task_id])
            merged.append(sanitize_error_detail(payload))
        return merged
    extracted: list[dict[str, Any]] = []
    for result in response.tool_results:
        if result.get("tool_name") != "delegate_to_agent":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        errors = data.get("errors") if isinstance(data, dict) else []
        artifacts = data.get("artifacts") if isinstance(data, dict) else []
        extracted.append(
            sanitize_error_detail(
                {
                    "task_id": data.get("task_id"),
                    "target_agent_id": data.get("target_agent_id"),
                    "status": data.get("status"),
                    "run_id": data.get("run_id"),
                    "trace_id": data.get("trace_id"),
                    "artifact_count": len(artifacts) if isinstance(artifacts, list) else 0,
                    "error_codes": [
                        error.get("code")
                        for error in errors
                        if isinstance(error, dict) and error.get("code")
                    ],
                    "metadata": _safe_delegation_metadata(data.get("metadata")),
                }
            )
        )
    return extracted


def _delegated_task_metadata_by_id(response: AgentRunResponse) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for result in response.tool_results:
        if result.get("tool_name") != "delegate_to_agent":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        task_id = data.get("task_id") if isinstance(data, dict) else None
        task_metadata = data.get("metadata") if isinstance(data, dict) else None
        if isinstance(task_id, str) and isinstance(task_metadata, dict):
            metadata[task_id] = _safe_delegation_metadata(task_metadata)
    return metadata


def _safe_delegation_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed_keys = {
        "agent_communication",
        "agent_context",
        "child_context_budget",
        "correlation_id",
        "delegation_budget",
        "delegation_pairs",
        "endpoint_host",
        "latency_ms",
        "remote_context_id",
        "remote_status_state",
        "remote_task_id",
        "tool_result_refs",
        "transport",
    }
    return sanitize_error_detail({key: child for key, child in value.items() if key in allowed_keys})


def _identity_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "identity_source",
        "auth_bound_identity",
        "auth_context_source",
        "requested_user_id",
        "requested_session_id",
        "warnings",
    }
    return sanitize_error_detail({key: value[key] for key in allowed if key in value})


def _budget_payload(
    *,
    response: AgentRunResponse,
    delegated_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    provider_budget = _find_dict_by_key(response.model_dump(mode="python"), "provider_budget")
    child_budgets = [
        task.get("metadata", {}).get("child_context_budget")
        for task in delegated_tasks
        if isinstance(task.get("metadata"), dict) and isinstance(task.get("metadata", {}).get("child_context_budget"), dict)
    ]
    payload = {
        "provider_budget": provider_budget,
        "delegated_task_count": len(delegated_tasks),
        "child_context_budgets": child_budgets,
        "tool_call_count": len(response.tool_calls),
        "tool_result_count": len(response.tool_results),
    }
    return sanitize_error_detail(payload)


def _cost_payload(budget: dict[str, Any]) -> dict[str, Any]:
    provider_budget = budget.get("provider_budget")
    if not isinstance(provider_budget, dict):
        return {}
    return sanitize_error_detail(
        {
            "estimated_cost": provider_budget.get("estimated_cost"),
            "cost_unit": provider_budget.get("cost_unit"),
            "provider_call_count": provider_budget.get("provider_call_count"),
        }
    )


def _trace_budget(trace: RunSummary | TraceSummary) -> dict[str, Any]:
    context = trace.context if isinstance(trace.context, dict) else {}
    budget = context.get("budget")
    return sanitize_error_detail(budget if isinstance(budget, dict) else {})


def _failure_class(
    *,
    response: AgentRunResponse,
    route_decision: dict[str, Any],
    delegated_tasks: list[dict[str, Any]],
) -> str | None:
    if response.status != "failed" and not response.errors:
        return None
    route_status = route_decision.get("status")
    if route_status == "failed":
        return "router_failure"
    codes = {str(error.code) for error in response.errors}
    codes.update(
        str(code)
        for task in delegated_tasks
        for code in task.get("error_codes", [])
        if code
    )
    if any(code.startswith("PROVIDER_") or code.startswith("provider_") for code in codes):
        return "provider_failure"
    if any("TOOL" in code or "tool" in code for code in codes):
        return "tool_failure"
    if delegated_tasks and any(task.get("status") == "failed" for task in delegated_tasks):
        return "worker_failure"
    if route_decision.get("collaboration_mode") == "controller_delegate":
        return "controller_failure"
    return "router_failure"


def _find_dict_by_key(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        found = value.get(key)
        if isinstance(found, dict):
            return sanitize_error_detail(found)
        for item in value.values():
            nested = _find_dict_by_key(item, key)
            if nested:
                return nested
    if isinstance(value, list):
        for item in value:
            nested = _find_dict_by_key(item, key)
            if nested:
                return nested
    return {}


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = sanitize_error_message(value)
    return text or None


def _int_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
