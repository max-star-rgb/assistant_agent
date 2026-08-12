from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from assistant_agent.workflows.durable_graph_app import (
    DurableWorkflowGraphApp,
    WorkflowGraphExecutionIdentity,
    WorkflowGraphStreamPart,
)
from assistant_agent.workflows.graph_projection import (
    WorkflowGraphProjector,
    WorkflowHandle,
    WorkflowProductSnapshot,
)

from workflow_graph_probe import workflow_probe


def _identity() -> WorkflowGraphExecutionIdentity:
    return WorkflowGraphExecutionIdentity(
        workflow_id="wf-send",
        thread_id="workflow-thread-send",
        run_id="invoke-send",
        user_id="user-send",
        session_id="session-send",
        agent_id="agent-send",
    )


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value), set())
    return set()


def test_real_compiled_stream_projects_only_strict_product_facts(tmp_path) -> None:
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"collect": [], "write": ["collect"]}
    )
    app = DurableWorkflowGraphApp(graph)

    try:
        result = asyncio.run(
            app.arun(initial, identity=_identity(), context=context)
        )
        projector = WorkflowGraphProjector()
        projected_native_parts = tuple(
            event
            for part in result.parts
            if (event := projector.project_stream_part(part)) is not None
        )
        snapshot = projector.project_snapshot(result.final_state)
        product_fact = projector.project_event(result.final_state)
        projected_fact = projector.project_stream_part(
            type(result.parts[0])(
                type="custom",
                namespace=(),
                data=product_fact.model_dump(mode="python"),
            )
        )

        assert result.status == "completed"
        assert snapshot.handle == WorkflowHandle(
            workflow_id="wf-send",
            workflow_type="deep_research",
            status="completed",
            phase="completed",
            output_ref="workflow://wf-send",
        )
        assert snapshot.progress.completed_items == 2
        assert snapshot.progress.total_items == 2
        assert snapshot.progress.active_items == ()
        assert projected_native_parts == ()
        assert projected_fact == product_fact

        public = {
            "snapshot": snapshot.model_dump(mode="json"),
            "event": product_fact.model_dump(mode="json"),
        }
        forbidden = {
            "checkpoint_id",
            "checkpoint_ns",
            "task_id",
            "namespace",
            "native_interrupt_id",
            "raw_body",
            "provider_response",
            "identity",
            "submission",
            "result_ledger",
            "publish_commit_ref",
        }
        assert not (_keys(public) & forbidden)
        assert "checkpoint" not in json.dumps(public, ensure_ascii=False).lower()
    finally:
        artifact_store.close()


def test_product_dtos_reject_native_or_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowHandle.model_validate(
            {
                "workflow_id": "wf-strict",
                "workflow_type": "deep_research",
                "status": "running",
                "phase": "executing",
                "output_ref": "workflow://wf-strict",
                "checkpoint_id": "native-secret",
            }
        )

    projector = WorkflowGraphProjector()
    assert projector.project_stream_part(
        WorkflowGraphStreamPart(
            type="custom",
            namespace=(),
            data={
                "schema_version": "workflow_product_event_v1",
                "checkpoint_id": "native-secret",
            },
        )
    ) is None

    with pytest.raises(ValidationError):
        WorkflowProductSnapshot.model_validate(
            {
                "handle": {
                    "workflow_id": "wf-strict",
                    "workflow_type": "deep_research",
                    "status": "running",
                    "phase": "executing",
                    "output_ref": "workflow://wf-strict",
                },
                "progress": {
                    "state": "working",
                    "phase": "executing",
                    "completed_items": 0,
                    "total_items": 1,
                    "active_items": [],
                },
                "result_artifact_refs": [],
                "waiting_actions": [],
                "raw_body": {"provider_response": "secret"},
            }
        )
