"""OpenTelemetry projection for durable Workflow lifecycle events."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from assistant_agent.observability.langfuse_config import local_langfuse_trace_url
from assistant_agent.observability.otel_exporter import (
    BufferedTextOtelSpanExporter,
    OtlpHttpTextExporterConfig,
    TextOtelSpanExporter,
    create_otlp_http_text_span_exporter,
)
from assistant_agent.observability.otel_mapping import OtelSpanSpec, langfuse_trace_id
from assistant_agent.observability.workflow_trace import workflow_root_span_id
from assistant_agent.workflows.models import WorkflowBundle, WorkflowEvent


class WorkflowCommitObserver(Protocol):
    def observe(
        self,
        bundle: WorkflowBundle,
        events: Sequence[WorkflowEvent],
    ) -> None: ...

    def close(self) -> None: ...


class WorkflowOtelObserver:
    """Export committed Workflow events without affecting Workflow durability."""

    def __init__(self, exporter: TextOtelSpanExporter) -> None:
        self.exporter = exporter

    def observe(
        self,
        bundle: WorkflowBundle,
        events: Sequence[WorkflowEvent],
    ) -> None:
        try:
            spans = build_workflow_otel_span_specs(bundle, events)
            if spans:
                self.exporter.export(spans)
        except Exception:  # noqa: BLE001 - observability must fail open.
            return

    def close(self) -> None:
        close = getattr(self.exporter, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - observability must fail open.
                return


def create_workflow_otel_observer_from_env(
    env: Mapping[str, str] | None = None,
) -> WorkflowOtelObserver | None:
    """Create the optional Workflow observer from the shared OTLP settings."""

    config = OtlpHttpTextExporterConfig.from_env(env)
    setup = create_otlp_http_text_span_exporter(config)
    if setup.status != "ready" or setup.exporter is None:
        return None
    return WorkflowOtelObserver(
        BufferedTextOtelSpanExporter(
            setup.exporter,
            capacity=config.queue_capacity,
        )
    )


def build_workflow_otel_span_specs(
    bundle: WorkflowBundle,
    events: Sequence[WorkflowEvent],
) -> list[OtelSpanSpec]:
    """Map committed plan, attempt, and terminal facts to one overview trace."""

    workflow = bundle.workflow
    export_trace_id = _workflow_export_trace_id(bundle)
    root_span_id = workflow_root_span_id(export_trace_id)
    common = {
        "langfuse.trace.name": f"{workflow.workflow_type}.workflow",
        "assistant_agent.trace_kind": "workflow",
        "assistant_agent.workflow_id": workflow.workflow_id,
        "assistant_agent.workflow_type": workflow.workflow_type,
        "assistant_agent.session_id": workflow.session_id,
        "langfuse.user.id": workflow.user_id,
        "langfuse.session.id": workflow.session_id,
    }
    spans: list[OtelSpanSpec] = []
    for event in events:
        if event.event_type == "workflow.accepted":
            spans.append(_root_span(bundle, event, root_span_id, common))
        if event.event_type in {"workflow.plan.created", "workflow.plan.revised"}:
            spans.append(_plan_span(bundle, event, root_span_id, common))
        if _is_work_item_event(event):
            if not event.payload.get("assistant_run_id"):
                spans.append(_work_item_span(bundle, event, root_span_id, common))
            elif event.event_type not in {
                "workflow.work_item.succeeded",
                "workflow.failed",
            }:
                spans.append(_work_item_state_span(bundle, event, root_span_id, common))
        if event.event_type in {
            "workflow.completed",
            "workflow.failed",
            "workflow.cancelled",
        }:
            spans.append(_terminal_span(bundle, event, root_span_id, common))
    return spans


def _root_span(
    bundle: WorkflowBundle,
    event: WorkflowEvent,
    root_span_id: str,
    common: dict[str, str],
) -> OtelSpanSpec:
    """Export one immutable root anchor when the Workflow is accepted."""

    workflow = bundle.workflow
    root_input = {
        "workflow_type": workflow.workflow_type,
        "deliverable_count": len(workflow.deliverables),
        "constraint_count": len(workflow.constraints),
    }
    return OtelSpanSpec(
        trace_id=_workflow_export_trace_id(bundle),
        span_id=root_span_id,
        parent_span_id=workflow.ingress_parent_span_id,
        name=f"{workflow.workflow_type}.workflow",
        start_time=event.created_at,
        end_time=event.created_at,
        status="unset",
        attributes={
            **common,
            "langfuse.observation.type": "agent",
            "assistant_agent.canonical_event": "workflow.runtime",
            "langfuse.observation.input": _json_value(root_input),
            **(
                {}
                if workflow.ingress_trace_id is not None
                else {"langfuse.trace.input": _json_value(root_input)}
            ),
        },
    )


def _terminal_span(
    bundle: WorkflowBundle,
    event: WorkflowEvent,
    root_span_id: str,
    common: dict[str, str],
) -> OtelSpanSpec:
    workflow = bundle.workflow
    return OtelSpanSpec(
        trace_id=_workflow_export_trace_id(bundle),
        span_id=_stable_span_id(workflow.workflow_id, f"terminal:{event.cursor}"),
        parent_span_id=root_span_id,
        name=event.event_type,
        start_time=event.created_at,
        end_time=event.created_at,
        status="ok" if event.event_type == "workflow.completed" else "error",
        attributes={
            **common,
            "langfuse.observation.type": "event",
            "assistant_agent.canonical_event": event.event_type,
            "assistant_agent.terminal_status": workflow.status,
            "langfuse.observation.output": _json_value(
                {
                    "status": workflow.status,
                    "terminal_reason_code": workflow.terminal_reason_code,
                    "result_artifact_refs": workflow.result_artifact_refs,
                }
            ),
        },
    )


def _work_item_state_span(
    bundle: WorkflowBundle,
    event: WorkflowEvent,
    root_span_id: str,
    common: dict[str, str],
) -> OtelSpanSpec:
    payload = event.payload
    return OtelSpanSpec(
        trace_id=_workflow_export_trace_id(bundle),
        span_id=_stable_span_id(
            bundle.workflow.workflow_id,
            f"work-item-state:{event.cursor}",
        ),
        parent_span_id=root_span_id,
        name=event.event_type,
        start_time=event.created_at,
        end_time=event.created_at,
        status=(
            "error"
            if event.event_type == "workflow.work_item.retry_scheduled"
            else "unset"
        ),
        attributes={
            **common,
            "langfuse.observation.type": "event",
            "assistant_agent.canonical_event": event.event_type,
            "assistant_agent.work_item_id": str(
                payload.get("work_item_id", "")
            ),
            "assistant_agent.attempt_id": str(payload.get("attempt_id", "")),
            "assistant_agent.agent_role": str(payload.get("agent_role", "worker")),
            "langfuse.observation.output": _json_value(
                {
                    "status": payload.get("work_item_status", event.status),
                    "execution_status": payload.get("execution_status"),
                    "error_code": payload.get("error_code"),
                    "repair_work_item_ids": payload.get(
                        "repair_work_item_ids",
                        [],
                    ),
                }
            ),
        },
    )


def _plan_span(
    bundle: WorkflowBundle,
    event: WorkflowEvent,
    root_span_id: str,
    common: dict[str, str],
) -> OtelSpanSpec:
    version = int(
        event.payload.get("plan_version", bundle.workflow.current_plan_version)
    )
    plan = next(
        (candidate for candidate in bundle.plans if candidate.version == version),
        bundle.current_plan,
    )
    return OtelSpanSpec(
        trace_id=_workflow_export_trace_id(bundle),
        span_id=_stable_span_id(bundle.workflow.workflow_id, f"plan:{event.cursor}"),
        parent_span_id=root_span_id,
        name="workflow.plan",
        start_time=event.created_at,
        end_time=event.created_at,
        status="ok",
        attributes={
            **common,
            "langfuse.observation.type": "chain",
            "assistant_agent.canonical_event": event.event_type,
            "assistant_agent.plan_version": plan.version,
            "langfuse.observation.input": _json_value(
                {
                    "revision_reason": plan.revision_reason,
                    "work_items": [
                        {
                            "work_item_id": item.work_item_id,
                            "kind": item.kind,
                            "display_title": item.display_title,
                            "depends_on": item.depends_on,
                            "status": item.status,
                        }
                        for item in plan.work_items
                    ],
                }
            ),
            "langfuse.observation.output": _json_value(
                {"status": event.status, "plan_version": plan.version}
            ),
        },
    )


def _work_item_span(
    bundle: WorkflowBundle,
    event: WorkflowEvent,
    root_span_id: str,
    common: dict[str, str],
) -> OtelSpanSpec:
    payload = event.payload
    work_item_id = str(payload["work_item_id"])
    plan_version = payload.get("plan_version")
    matching_plans = (
        [plan for plan in bundle.plans if plan.version == plan_version]
        if isinstance(plan_version, int)
        else list(reversed(bundle.plans))
    )
    work_item = next(
        (
            item
            for plan in matching_plans
            for item in plan.work_items
            if item.work_item_id == work_item_id
        ),
        None,
    )
    assistant_trace_id = payload.get("assistant_trace_id")
    workflow_trace_id = _workflow_export_trace_id(bundle)
    output = {
        "status": payload.get("work_item_status", event.status),
        "execution_status": payload.get("execution_status"),
        "event_type": event.event_type,
        "attempt_id": payload.get("attempt_id"),
        "attempt_count": payload.get("attempt_count"),
        "error_code": payload.get("error_code"),
        "artifact_refs": payload.get("artifact_refs", []),
        "assistant_canonical_trace_id": assistant_trace_id,
        "assistant_run_id": payload.get("assistant_run_id"),
        "workflow_trace_id": workflow_trace_id,
        "workflow_trace_url": local_langfuse_trace_url(workflow_trace_id),
    }
    return OtelSpanSpec(
        trace_id=_workflow_export_trace_id(bundle),
        span_id=(
            workflow_attempt_span_id(
                bundle.workflow.workflow_id,
                str(payload["attempt_id"]),
            )
            if payload.get("attempt_id")
            else _stable_span_id(
                bundle.workflow.workflow_id,
                f"work-item:{event.cursor}",
            )
        ),
        parent_span_id=root_span_id,
        name=(
            str(work_item.display_title)
            if work_item is not None and work_item.display_title
            else f"workflow.work_item.{work_item.kind if work_item else 'unknown'}"
        ),
        start_time=_event_time(payload.get("started_at"), event.created_at),
        end_time=_event_time(payload.get("finished_at"), event.created_at),
        status=(
            "ok"
            if event.event_type == "workflow.work_item.succeeded"
            else "error"
            if event.event_type == "workflow.failed"
            else "unset"
        ),
        attributes={
            **common,
            "langfuse.observation.type": "chain",
            "assistant_agent.canonical_event": event.event_type,
            "assistant_agent.work_item_id": work_item_id,
            "assistant_agent.attempt_id": str(payload.get("attempt_id", "")),
            "assistant_agent.agent_role": str(
                payload.get("agent_role", "worker")
            ),
            "langfuse.observation.input": _json_value(
                {
                    "work_item_id": work_item_id,
                    "kind": work_item.kind if work_item else None,
                    "display_title": work_item.display_title if work_item else None,
                    "depends_on": work_item.depends_on if work_item else [],
                }
            ),
            "langfuse.observation.output": _json_value(output),
        },
    )


def _is_work_item_event(event: WorkflowEvent) -> bool:
    return "work_item_id" in event.payload and event.event_type in {
        "workflow.work_item.succeeded",
        "workflow.work_item.retry_scheduled",
        "workflow.repair.requested",
        "workflow.input.required",
        "workflow.failed",
    }


def _workflow_export_trace_id(bundle: WorkflowBundle) -> str:
    return langfuse_trace_id(
        bundle.workflow.ingress_trace_id or bundle.workflow.workflow_id
    )


def _stable_span_id(workflow_id: str, suffix: str) -> str:
    value = sha256(f"{workflow_id}:{suffix}".encode()).digest()[:8].hex()
    return value if int(value, 16) != 0 else "0000000000000001"


def workflow_attempt_span_id(workflow_id: str, attempt_id: str) -> str:
    """Return the durable parent span identity known before Agent execution."""

    return _stable_span_id(workflow_id, f"attempt:{attempt_id}")


def _event_time(value: object, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return fallback


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
