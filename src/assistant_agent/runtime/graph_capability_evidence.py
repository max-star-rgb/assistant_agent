"""Machine-readable evidence matrix for the native LangGraph M5 surface."""

from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


GraphCapability = Literal[
    "StateGraph",
    "State",
    "Node",
    "Edge",
    "START",
    "END",
    "Conditional Edge",
    "Command",
    "Send",
    "Reducer",
    "Subgraph",
    "Pregel / Super-step",
    "Compile",
    "Invoke",
    "Stream",
    "Checkpoint",
    "Checkpointer",
    "Thread",
    "Interrupt",
    "Resume",
    "Memory",
    "Store",
    "Runtime Context",
    "Retry Policy",
    "Timeout",
    "Fallback",
    "Streaming Modes",
    "Time Travel",
    "Replay",
    "Fork",
]
GraphCapabilityStatus = Literal["implemented", "not_applicable"]
GraphEvidenceKind = Literal[
    "source_contract",
    "contract_test",
    "integration_test",
    "negative_contract_test",
]
GraphAcceptanceGate = Literal["P1", "P2", "P3", "P4"]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GraphCapabilityEvidence(_StrictFrozenModel):
    """One hard-constraint capability and its repository-owned proof."""

    capability: GraphCapability
    status: GraphCapabilityStatus
    evidence_path: str = Field(pattern=r"^[A-Za-z0-9_./-]+(?:::[A-Za-z0-9_./-]+)?$")
    evidence_kind: GraphEvidenceKind
    gate: GraphAcceptanceGate


def evidence_anchor_is_defined(
    repo_root: str | Path,
    evidence: GraphCapabilityEvidence,
) -> bool:
    """Resolve one evidence anchor without importing or executing its module."""

    root = Path(repo_root).resolve()
    relative_path, anchor = evidence.evidence_path.split("::", 1)
    try:
        source_path = (root / relative_path).resolve(strict=True)
        source_path.relative_to(root)
        module = ast.parse(source_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, SyntaxError, UnicodeError, ValueError):
        return False

    definitions: set[str] = set()
    for statement in module.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if evidence.evidence_kind == "source_contract" or statement.name.startswith(
                "test_"
            ):
                definitions.add(statement.name)
            continue
        if evidence.evidence_kind != "source_contract":
            continue
        if isinstance(statement, ast.Assign):
            definitions.update(
                target.id
                for target in statement.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            definitions.add(statement.target.id)
    return anchor in definitions


def _implemented(
    capability: GraphCapability,
    evidence_path: str,
    evidence_kind: GraphEvidenceKind,
    gate: GraphAcceptanceGate,
) -> GraphCapabilityEvidence:
    return GraphCapabilityEvidence(
        capability=capability,
        status="implemented",
        evidence_path=evidence_path,
        evidence_kind=evidence_kind,
        gate=gate,
    )


GRAPH_CAPABILITY_EVIDENCE: tuple[GraphCapabilityEvidence, ...] = (
    _implemented(
        "StateGraph",
        "src/assistant_agent/runtime/assistant_loop_graph.py::build_assistant_loop_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "State",
        "src/assistant_agent/runtime/assistant_graph_state.py::AssistantTurnState",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Node",
        "src/assistant_agent/runtime/assistant_loop_graph.py::build_assistant_loop_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Edge",
        "src/assistant_agent/runtime/assistant_loop_graph.py::build_assistant_loop_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "START",
        "src/assistant_agent/runtime/assistant_loop_graph.py::build_assistant_loop_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "END",
        "src/assistant_agent/workflows/durable_graph.py::build_durable_workflow_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Conditional Edge",
        "src/assistant_agent/workflows/durable_graph.py::build_durable_workflow_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Command",
        "src/assistant_agent/workflows/durable_graph_nodes.py::decide_verification_node",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Send",
        "src/assistant_agent/workflows/durable_graph_nodes.py::route_next_wave",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Reducer",
        "src/assistant_agent/workflows/graph_state.py::DurableWorkflowState",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Subgraph",
        "src/assistant_agent/workflows/planning_graph.py::build_workflow_planning_subgraph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Pregel / Super-step",
        "tests/tdd/native-langgraph-m3/test_workflow_send_join.py::test_send_runs_ready_nodes_in_one_superstep_and_join_waits_for_all",
        "integration_test",
        "P1",
    ),
    _implemented(
        "Compile",
        "src/assistant_agent/workflows/durable_graph.py::build_durable_workflow_graph",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Invoke",
        "tests/tdd/native-langgraph-m2/test_runtime_resume.py::test_internal_runtime_waits_then_rebuilt_resume_commits_one_terminal",
        "integration_test",
        "P1",
    ),
    _implemented(
        "Stream",
        "tests/tdd/native-langgraph-m5/test_graph_stream_subscription.py::test_assistant_and_workflow_pass_one_subscription_to_v2_streams",
        "integration_test",
        "P3",
    ),
    _implemented(
        "Checkpoint",
        "tests/tdd/native-langgraph-m5/test_checkpoint_history.py::test_history_returns_opaque_selector_without_native_ids",
        "contract_test",
        "P1",
    ),
    _implemented(
        "Checkpointer",
        "tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py::test_async_owner_opens_official_sqlite_saver_and_business_claim_store",
        "integration_test",
        "P1",
    ),
    _implemented(
        "Thread",
        "tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py::test_fresh_persistent_hosts_resume_history_replay_and_fork",
        "integration_test",
        "P1",
    ),
    _implemented(
        "Interrupt",
        "tests/tdd/native-langgraph-m2/test_runtime_resume.py::test_internal_runtime_waits_then_rebuilt_resume_commits_one_terminal",
        "integration_test",
        "P1",
    ),
    _implemented(
        "Resume",
        "tests/tdd/native-langgraph-m5/test_persistent_checkpointer.py::test_fresh_persistent_hosts_resume_history_replay_and_fork",
        "integration_test",
        "P1",
    ),
    _implemented(
        "Memory",
        "tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py::test_long_term_memory_content_is_not_checkpointed",
        "negative_contract_test",
        "P4",
    ),
    GraphCapabilityEvidence(
        capability="Store",
        status="not_applicable",
        evidence_path=(
            "tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py"
            "::test_workflow_graph_does_not_compile_an_unused_store"
        ),
        evidence_kind="negative_contract_test",
        gate="P4",
    ),
    _implemented(
        "Runtime Context",
        "src/assistant_agent/runtime/graph_runtime.py::GraphRuntimeContext",
        "source_contract",
        "P1",
    ),
    _implemented(
        "Retry Policy",
        "src/assistant_agent/workflows/durable_graph_nodes.py::WORKFLOW_TRANSIENT_RETRY_POLICY",
        "source_contract",
        "P2",
    ),
    _implemented(
        "Timeout",
        "src/assistant_agent/workflows/durable_graph_nodes.py::WORKFLOW_NODE_TIMEOUT",
        "source_contract",
        "P2",
    ),
    _implemented(
        "Fallback",
        "src/assistant_agent/workflows/durable_graph_nodes.py::workflow_node_error_handler",
        "source_contract",
        "P2",
    ),
    _implemented(
        "Streaming Modes",
        "tests/tdd/native-langgraph-m5/test_graph_stream_subscription.py::test_subscription_and_parser_enforce_the_native_v2_allowlist",
        "contract_test",
        "P3",
    ),
    _implemented(
        "Time Travel",
        "tests/tdd/native-langgraph-m5/test_assistant_time_travel_facade.py::test_time_travel_effect_preflight_precedes_product_lifecycle",
        "integration_test",
        "P3",
    ),
    _implemented(
        "Replay",
        "tests/tdd/native-langgraph-m5/test_graph_replay.py::test_replay_uses_historical_config_and_unified_stream_consumer",
        "integration_test",
        "P3",
    ),
    _implemented(
        "Fork",
        "tests/tdd/native-langgraph-m5/test_graph_fork.py::test_fork_uses_exact_historical_config_public_update_and_returned_branch",
        "integration_test",
        "P3",
    ),
)


class GraphM5DeliveryEvidence(_StrictFrozenModel):
    """Overall delivery gate kept separate from implemented capabilities."""

    retirement_ready: bool
    nonterminal_legacy_count: int = Field(ge=0)
    waiting_legacy_count: int = Field(ge=0)
    active_legacy_lease_count: int = Field(ge=0)
    evidence_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_retirement_counts(self) -> "GraphM5DeliveryEvidence":
        if self.retirement_ready and any(
            (
                self.nonterminal_legacy_count,
                self.waiting_legacy_count,
                self.active_legacy_lease_count,
            )
        ):
            raise ValueError("ready retirement evidence requires zero legacy counts")
        return self

    @computed_field
    @property
    def status(self) -> Literal["accepted", "blocked"]:
        return "accepted" if self.retirement_ready else "blocked"

    @computed_field
    @property
    def reason_codes(self) -> tuple[Literal["legacy_retirement_gate_open"], ...]:
        return () if self.retirement_ready else ("legacy_retirement_gate_open",)


class _WorkflowRetirementProbeReport(_StrictFrozenModel):
    schema_version: Literal["workflow_retirement_probe_v1"]
    observed_at: datetime
    probe_mode: Literal["sqlite_read_only"]
    operator_manifest_available: bool
    ready: bool
    nonterminal_legacy_count: int = Field(ge=0)
    active_legacy_lease_count: int = Field(ge=0)
    waiting_legacy_count: int = Field(ge=0)
    legacy_status_counts: dict[str, int]
    persisted_retirement_audit_count: int = Field(ge=0)
    gate_blocker: Literal["operator_manifest_unavailable"]
    database_metadata_unchanged: bool

    @model_validator(mode="after")
    def validate_probe(self) -> "_WorkflowRetirementProbeReport":
        if self.observed_at.tzinfo is None:
            raise ValueError("retirement probe timestamp must be timezone-aware")
        if self.ready or self.operator_manifest_available:
            raise ValueError(
                "blocked probe must not claim an available retired manifest"
            )
        if not self.database_metadata_unchanged:
            raise ValueError("retirement probe changed the operator database")
        if self.nonterminal_legacy_count != sum(
            count
            for status, count in self.legacy_status_counts.items()
            if status not in {"completed", "failed", "cancelled"}
        ):
            raise ValueError("retirement probe nonterminal count is inconsistent")
        return self


def load_graph_m5_delivery_evidence(path: str | Path) -> GraphM5DeliveryEvidence:
    """Load the recorded read-only retirement probe without touching its database."""

    evidence_path = Path(path)
    report = _WorkflowRetirementProbeReport.model_validate_json(
        evidence_path.read_text(encoding="utf-8")
    )
    return GraphM5DeliveryEvidence(
        retirement_ready=report.ready,
        nonterminal_legacy_count=report.nonterminal_legacy_count,
        waiting_legacy_count=report.waiting_legacy_count,
        active_legacy_lease_count=report.active_legacy_lease_count,
        evidence_path=evidence_path.as_posix(),
    )


__all__ = [
    "GRAPH_CAPABILITY_EVIDENCE",
    "GraphCapabilityEvidence",
    "GraphM5DeliveryEvidence",
    "evidence_anchor_is_defined",
    "load_graph_m5_delivery_evidence",
]
