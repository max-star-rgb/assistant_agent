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
    WorkflowProductEvent,
    WorkflowProductSnapshot,
    WorkflowWaitingAction,
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
                data=product_fact.model_dump(mode="json"),
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


def test_compiled_interrupt_projects_waiting_input_instead_of_failure(tmp_path) -> None:
    blocked = json.dumps(
        {
            "workflow_control": {
                "outcome": "blocked",
                "summary": "need answer",
                "required_fields": ["answer"],
                "prompt_code": "need_answer",
                "safe_prompt": "Provide the answer.",
            }
        }
    )
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"collect": []}, worker_responses={"collect": blocked}
    )
    app = DurableWorkflowGraphApp(graph)

    try:
        result = asyncio.run(app.arun(initial, identity=_identity(), context=context))
        projector = WorkflowGraphProjector()
        snapshot = projector.project_snapshot(result.final_state)
        event = projector.project_event(result.final_state)

        assert result.status == "interrupted"
        assert snapshot.handle.status == "blocked"
        assert snapshot.handle.phase == "waiting_input"
        assert snapshot.progress.state == "waiting_input"
        assert len(snapshot.waiting_actions) == 1
        assert event.event_type == "waiting_input"
        assert event.terminal_reason_code is None
    finally:
        artifact_store.close()


def test_product_custom_fact_enforces_refs_actions_consistency_and_digest(
    tmp_path,
) -> None:
    graph, context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"collect": []}
    )
    app = DurableWorkflowGraphApp(graph)
    try:
        result = asyncio.run(app.arun(initial, identity=_identity(), context=context))
        projector = WorkflowGraphProjector()
        valid = projector.project_event(result.final_state)
        inconsistent = valid.model_dump(mode="json")
        inconsistent["status"] = "running"
        assert projector.project_stream_part(
            WorkflowGraphStreamPart(type="custom", namespace=(), data=inconsistent)
        ) is None
        wrong_digest = valid.model_copy(
            update={"event_id": "workflow-event:sha256:" + "0" * 64}
        )
        with pytest.raises(ValidationError):
            WorkflowProductEvent.model_validate_json(wrong_digest.model_dump_json())
    finally:
        artifact_store.close()

    with pytest.raises(ValidationError):
        WorkflowWaitingAction(
            action_ref="native-interrupt-123",
            node_id="collect",
            required_fields=("answer",),
            prompt_code="need_answer",
            safe_prompt="Provide the answer.",
        )
    with pytest.raises(ValidationError):
        WorkflowWaitingAction(
            action_ref="workflow:wf-send:node:collect:generation:0",
            node_id="collect",
            required_fields=("answer",),
            prompt_code="need_answer",
            safe_prompt="Read file:///home/user/raw-response.json",
        )
    with pytest.raises(ValidationError):
        WorkflowProductSnapshot(
            handle=WorkflowHandle(
                workflow_id="wf-send",
                workflow_type="deep_research",
                status="completed",
                phase="completed",
                output_ref="workflow://wf-send",
            ),
            progress={
                "state": "completed",
                "phase": "completed",
                "completed_items": 1,
                "total_items": 1,
                "active_items": (),
            },
            result_artifact_refs=("file:///home/user/report.txt",),
        )

    with pytest.raises(ValidationError):
        WorkflowProductSnapshot(
            handle=WorkflowHandle(
                workflow_id="wf-send",
                workflow_type="deep_research",
                status="completed",
                phase="failed",
                output_ref="workflow://wf-send",
            ),
            progress={
                "state": "working",
                "phase": "failed",
                "completed_items": 0,
                "total_items": 1,
                "active_items": (),
            },
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


def test_pre_admission_failure_projects_safe_failed_terminal(tmp_path) -> None:
    _graph, _context, initial, _worker, artifact_store = workflow_probe(
        tmp_path, {"collect": []}
    )
    failed = dict(initial)
    failed.update(status="failed", phase="failed")
    try:
        projector = WorkflowGraphProjector()
        snapshot = projector.project_snapshot(failed)
        event = projector.project_event(failed)

        assert snapshot.progress.state == "failed"
        assert snapshot.progress.total_items == 0
        assert snapshot.terminal_reason_code == "workflow_failed"
        assert event.event_type == "failed"
    finally:
        artifact_store.close()
