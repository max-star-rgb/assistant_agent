from __future__ import annotations

from pathlib import Path
import subprocess


REQUIRED = {
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
}


def test_final_graph_api_matrix_is_complete_and_machine_readable() -> None:
    """Dropping or weakening any hard-constraint capability must fail acceptance."""

    from assistant_agent.runtime.graph_capability_evidence import (
        GRAPH_CAPABILITY_EVIDENCE,
        GraphCapabilityEvidence,
        evidence_anchor_is_defined,
    )

    assert all(
        isinstance(item, GraphCapabilityEvidence) for item in GRAPH_CAPABILITY_EVIDENCE
    )
    assert {item.capability for item in GRAPH_CAPABILITY_EVIDENCE} == REQUIRED
    assert len(GRAPH_CAPABILITY_EVIDENCE) == len(REQUIRED)
    assert not [
        item
        for item in GRAPH_CAPABILITY_EVIDENCE
        if item.status not in {"implemented", "not_applicable"}
    ]
    assert {
        item.capability
        for item in GRAPH_CAPABILITY_EVIDENCE
        if item.status == "not_applicable"
    } == {"Store"}
    assert {item.gate for item in GRAPH_CAPABILITY_EVIDENCE} == {"P1", "P2", "P3", "P4"}

    repo_root = Path(__file__).resolve().parents[3]
    tracked = set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    for item in GRAPH_CAPABILITY_EVIDENCE:
        path, anchor = item.evidence_path.split("::", 1)
        assert path in tracked, item
        evidence = repo_root / path
        assert evidence.is_file(), item
        assert evidence_anchor_is_defined(repo_root, item), item


def test_store_is_not_applicable_only_without_a_graph_store_consumer() -> None:
    """A compile-time Store claim requires a real cross-node runtime consumer."""

    from assistant_agent.runtime.graph_capability_evidence import (
        GRAPH_CAPABILITY_EVIDENCE,
    )

    store = next(
        item for item in GRAPH_CAPABILITY_EVIDENCE if item.capability == "Store"
    )
    assert store.status == "not_applicable"
    assert store.evidence_kind == "negative_contract_test"
    assert store.evidence_path == (
        "tests/tdd/native-langgraph-m5/test_memory_store_boundaries.py"
        "::test_workflow_graph_does_not_compile_an_unused_store"
    )


def test_evidence_anchor_rejects_comments_imports_and_plain_references(
    tmp_path,
) -> None:
    """Evidence must be a definition, not text that happens to name a symbol."""

    from assistant_agent.runtime.graph_capability_evidence import (
        GraphCapabilityEvidence,
        evidence_anchor_is_defined,
    )

    source = tmp_path / "source_probe.py"
    source.write_text(
        "# forged_source_anchor\n"
        "from elsewhere import imported_anchor\n"
        "value = plain_reference_anchor\n"
        "real_assignment = 1\n"
        "async def real_async_function():\n"
        "    return None\n"
        "class RealClass:\n"
        "    pass\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_probe.py"
    test_file.write_text(
        "# test_forged_comment\n"
        "test_forged_reference = helper\n"
        "def helper():\n"
        "    return None\n"
        "def test_real_case():\n"
        "    return None\n",
        encoding="utf-8",
    )

    def item(path: str, anchor: str, kind: str) -> GraphCapabilityEvidence:
        return GraphCapabilityEvidence(
            capability="StateGraph",
            status="implemented",
            evidence_path=f"{path}::{anchor}",
            evidence_kind=kind,
            gate="P1",
        )

    assert not evidence_anchor_is_defined(
        tmp_path, item("source_probe.py", "forged_source_anchor", "source_contract")
    )
    assert not evidence_anchor_is_defined(
        tmp_path, item("source_probe.py", "imported_anchor", "source_contract")
    )
    assert not evidence_anchor_is_defined(
        tmp_path, item("source_probe.py", "plain_reference_anchor", "source_contract")
    )
    assert evidence_anchor_is_defined(
        tmp_path, item("source_probe.py", "real_assignment", "source_contract")
    )
    assert evidence_anchor_is_defined(
        tmp_path, item("source_probe.py", "real_async_function", "source_contract")
    )
    assert evidence_anchor_is_defined(
        tmp_path, item("source_probe.py", "RealClass", "source_contract")
    )
    assert not evidence_anchor_is_defined(
        tmp_path, item("test_probe.py", "test_forged_comment", "contract_test")
    )
    assert not evidence_anchor_is_defined(
        tmp_path, item("test_probe.py", "test_forged_reference", "integration_test")
    )
    assert not evidence_anchor_is_defined(
        tmp_path, item("test_probe.py", "helper", "negative_contract_test")
    )
    assert evidence_anchor_is_defined(
        tmp_path, item("test_probe.py", "test_real_case", "contract_test")
    )


def test_delivery_gate_accepts_persisted_retirement_without_weakening_capabilities() -> (
    None
):
    """Persisted operator retirement closes delivery while preserving capability facts."""

    from assistant_agent.runtime.graph_capability_evidence import (
        GRAPH_CAPABILITY_EVIDENCE,
        load_graph_m5_delivery_evidence,
    )

    repo_root = Path(__file__).resolve().parents[3]
    relative_evidence_path = (
        ".superpowers/sdd/2026-08-13-native-langgraph-m5/task-9-retirement-status.json"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", relative_evidence_path],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert tracked == relative_evidence_path
    delivery = load_graph_m5_delivery_evidence(repo_root / relative_evidence_path)
    assert delivery.status == "accepted"
    assert delivery.reason_codes == ()
    assert delivery.nonterminal_legacy_count == 0
    assert delivery.waiting_legacy_count == 0
    assert delivery.active_legacy_lease_count == 0
    assert delivery.evidence_path.endswith("task-9-retirement-status.json")
    assert all(
        item.status in {"implemented", "not_applicable"}
        for item in GRAPH_CAPABILITY_EVIDENCE
    )
