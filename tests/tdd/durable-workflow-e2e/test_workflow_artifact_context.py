from __future__ import annotations

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.workflows.agent_runtime import AgentWorkItemRequest
from assistant_agent.workflows.artifacts import (
    ArtifactAccessDenied,
    LocalWorkflowArtifactStore,
)
from assistant_agent.workflows.context import WorkflowContextCompiler


class _CharacterTokenCounter:
    tokenizer_id = "character-sentinel"

    def count_text(self, value: str) -> int:
        return len(value)


def _identity(*, user_id: str = "user-sentinel") -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id=user_id,
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )


def test_artifact_is_immutable_reopenable_and_owner_scoped(tmp_path) -> None:
    first = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    ref = first.write_text(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        kind="source",
        text="artifact-content-sentinel",
        producer_work_item_id="collect-sentinel",
    )
    duplicate = first.write_text(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        kind="source",
        text="artifact-content-sentinel",
        producer_work_item_id="collect-sentinel",
    )
    first.close()

    reopened = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    assert duplicate.artifact_id == ref.artifact_id
    assert reopened.read_text(identity=_identity(), artifact_ref=ref.uri) == (
        "artifact-content-sentinel"
    )
    with pytest.raises(ArtifactAccessDenied):
        reopened.read_text(
            identity=_identity(user_id="other-user"),
            artifact_ref=ref.uri,
        )
    reopened.close()


def test_context_manifest_contains_bounded_excerpts_not_full_artifacts(tmp_path) -> None:
    store = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    ref = store.write_text(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        kind="source",
        text="A" * 500,
        producer_work_item_id="collect-sentinel",
    )

    manifest = WorkflowContextCompiler(
        artifact_store=store,
        max_total_chars=80,
        max_artifact_chars=60,
    ).compile(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        objective="objective-sentinel",
        constraints=["constraint-sentinel"],
        artifact_refs=[ref.uri],
    )

    assert manifest.artifacts[0].artifact_ref == ref.uri
    assert len(manifest.artifacts[0].excerpt) <= 60
    assert manifest.total_excerpt_chars <= 80
    assert manifest.trimmed is True
    store.close()


def test_default_context_budget_allows_one_long_draft_to_reach_the_next_item(
    tmp_path,
) -> None:
    store = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    content = "D" * 8_000
    ref = store.write_text(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        kind="draft",
        text=content,
        producer_work_item_id="draft-sentinel",
    )

    manifest = WorkflowContextCompiler(artifact_store=store).compile(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        objective="verify-sentinel",
        constraints=[],
        artifact_refs=[ref.uri],
    )

    assert manifest.artifacts[0].excerpt == content
    assert manifest.trimmed is False
    store.close()


def test_context_budget_is_stage_and_model_window_aware(tmp_path) -> None:
    store = LocalWorkflowArtifactStore(tmp_path / "artifacts")
    ref = store.write_text(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        kind="evidence",
        text="E" * 300_000,
        producer_work_item_id="evidence-sentinel",
    )
    compiler = WorkflowContextCompiler(
        artifact_store=store,
        token_counter=_CharacterTokenCounter(),
        model_context_window_tokens=1_000_000,
        output_reserve_tokens=32_000,
        safety_margin_tokens=50_000,
    )

    manifest = compiler.compile(
        identity=_identity(),
        workflow_id="workflow-sentinel",
        objective="synthesize-sentinel",
        constraints=[],
        artifact_refs=[ref.uri],
        work_item_kind="synthesize",
    )

    assert manifest.token_budget == 250_000
    assert manifest.total_excerpt_tokens == 250_000
    assert len(manifest.artifacts[0].excerpt) == 250_000
    assert manifest.trimmed is True
    store.close()


def test_agent_graph_runtime_exposes_bounded_work_item_entry() -> None:
    runtime = AgentGraphRuntime(config=ProviderConfig())
    request = AgentWorkItemRequest(
        workflow_id="workflow-sentinel",
        workflow_type="long_horizon",
        work_item_id="step-sentinel",
        attempt_id="attempt-sentinel",
        display_title="正在执行 Sentinel 步骤",
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
        objective="直接回答 work-item-sentinel。",
        context_manifest={
            "workflow_id": "workflow-sentinel",
            "objective": "objective-sentinel",
            "constraints": [],
            "artifacts": [],
            "total_excerpt_chars": 0,
            "trimmed": False,
        },
        allowed_tool_names=[],
        max_iterations=2,
    )

    result = runtime.run_work_item(request)

    assert result.status == "succeeded"
    assert result.summary
    assert result.run_id
