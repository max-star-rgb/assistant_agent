import json
from pathlib import Path

from assistant_agent.services.improvement.lab import run_improvement_lab
from assistant_agent.services.trace_store import InMemoryTraceStore, TraceEvent


def test_structured_eval_produces_skill_candidate_in_dry_run(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    eval_path = tmp_path / "eval.json"
    _write_source(
        eval_path,
        {
            "source_ref": "eval:search:case1",
            "target_type": "skill",
            "target_ref": "realtime_web_search",
            "symptom_code": "skill_tool_not_selected_in_eval",
            "summary": "Governed search was not selected in the offline eval.",
            "severity": "high",
            "attributes": {"rubric_code": "expected_tool_missing", "score": 0.0},
        },
    )

    report = run_improvement_lab(
        trace_store=None,
        run_ids=[],
        trace_ids=[],
        eval_paths=[eval_path],
        test_paths=[],
        target_type="skill",
        skill_id="realtime_web_search",
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=False,
        proposal_mode="deterministic",
    )

    assert len(report.opportunities) == 1
    assert len(report.candidates) == 1
    assert report.persisted is False
    assert not (tmp_path / "registry").exists()


def test_two_trace_runs_produce_one_runtime_opportunity(tmp_path: Path) -> None:
    store = InMemoryTraceStore()
    for index in (1, 2):
        store.append(
            TraceEvent(
                trace_id=f"trace_{index}",
                run_id=f"run_{index}",
                node_name="assistant_loop",
                event_type="loop_guard_triggered",
                canonical_event="loop.guard.triggered",
                status="assistant_loop_limit_reached",
                error_code="assistant_loop_limit_reached",
            )
        )

    report = run_improvement_lab(
        trace_store=store,
        run_ids=["run_1", "run_2"],
        trace_ids=[],
        eval_paths=[],
        test_paths=[],
        target_type="runtime",
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=False,
        proposal_mode="deterministic",
    )

    assert report.opportunities[0].recurrence_count == 2
    assert report.opportunities[0].status == "ready_for_proposal"
    assert report.candidates[0].status == "ready_for_review"


def test_invalid_source_does_not_discard_valid_source_and_persistence_is_explicit(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.json"
    invalid = tmp_path / "missing.json"
    _write_source(
        valid,
        {
            "source_ref": "pytest:case1",
            "target_type": "code",
            "target_ref": "assistant_agent.agent.runtime:AgentGraphRuntime",
            "symptom_code": "deterministic_test_regression",
            "summary": "The deterministic runtime regression failed.",
            "severity": "high",
            "attributes": {"module": "assistant_agent.agent.runtime", "symbol": "AgentGraphRuntime"},
        },
    )

    report = run_improvement_lab(
        trace_store=None,
        run_ids=[],
        trace_ids=[],
        eval_paths=[],
        test_paths=[invalid, valid],
        target_type=None,
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=True,
        proposal_mode="deterministic",
    )

    assert report.persisted is True
    assert report.candidates
    assert any(issue.code == "evidence_source_not_found" for issue in report.issues)
    assert (tmp_path / "registry" / "candidates.jsonl").exists()


def test_allowlisted_evals_do_not_run_unless_explicit(tmp_path: Path) -> None:
    calls = []

    class Completed:
        returncode = 0
        stdout = "passed"
        stderr = ""

    def runner(command, **kwargs):
        calls.append(command)
        return Completed()

    source = tmp_path / "test.json"
    _write_source(
        source,
        {
            "source_ref": "pytest:case2",
            "target_type": "code",
            "target_ref": "assistant_agent.agent.runtime:AgentGraphRuntime",
            "symptom_code": "deterministic_test_regression",
            "summary": "The deterministic runtime regression failed.",
            "severity": "high",
            "attributes": {"module": "assistant_agent.agent.runtime", "symbol": "AgentGraphRuntime"},
        },
    )

    without = run_improvement_lab(
        trace_store=None,
        run_ids=[],
        trace_ids=[],
        eval_paths=[],
        test_paths=[source],
        target_type="code",
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=False,
        proposal_mode="deterministic",
        eval_runner=runner,
    )
    with_eval = run_improvement_lab(
        trace_store=None,
        run_ids=[],
        trace_ids=[],
        eval_paths=[],
        test_paths=[source],
        target_type="code",
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=False,
        proposal_mode="deterministic",
        run_allowlisted_evals=True,
        eval_runner=runner,
    )

    assert without.validation_results == []
    assert len(with_eval.validation_results) == 1
    assert len(calls) == 1


def test_failed_allowlisted_eval_blocks_candidate_before_persistence(tmp_path: Path) -> None:
    class Completed:
        returncode = 1
        stdout = "failed"
        stderr = ""

    source = tmp_path / "test-failure.json"
    _write_source(
        source,
        {
            "source_ref": "pytest:case3",
            "target_type": "code",
            "target_ref": "assistant_agent.agent.runtime:AgentGraphRuntime",
            "symptom_code": "deterministic_test_regression",
            "summary": "The deterministic runtime regression failed.",
            "severity": "high",
            "attributes": {"module": "assistant_agent.agent.runtime", "symbol": "AgentGraphRuntime"},
        },
    )

    report = run_improvement_lab(
        trace_store=None,
        run_ids=[],
        trace_ids=[],
        eval_paths=[],
        test_paths=[source],
        target_type="code",
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=True,
        proposal_mode="deterministic",
        run_allowlisted_evals=True,
        eval_runner=lambda *_args, **_kwargs: Completed(),
    )

    assert report.candidates[0].status == "evaluation_failed"
    assert "allowlisted_eval_failed" in report.candidates[0].evaluation.blocked_reasons
    assert (tmp_path / "registry" / "validation_results.jsonl").exists()


def test_repeated_candidate_persists_each_run_evaluation(tmp_path: Path) -> None:
    source = tmp_path / "repeat.json"
    _write_source(
        source,
        {
            "source_ref": "pytest:repeat",
            "target_type": "code",
            "target_ref": "assistant_agent.agent.runtime:AgentGraphRuntime",
            "symptom_code": "deterministic_test_regression",
            "summary": "The deterministic runtime regression failed.",
            "severity": "high",
            "attributes": {"module": "assistant_agent.agent.runtime", "symbol": "AgentGraphRuntime"},
        },
    )
    common = dict(
        trace_store=None,
        run_ids=[],
        trace_ids=[],
        eval_paths=[],
        test_paths=[source],
        target_type="code",
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=True,
        proposal_mode="deterministic",
    )

    first = run_improvement_lab(**common)

    class Failed:
        returncode = 1
        stdout = "failed"
        stderr = ""

    second = run_improvement_lab(
        **common,
        run_allowlisted_evals=True,
        eval_runner=lambda *_args, **_kwargs: Failed(),
    )

    assert first.candidates[0].candidate_id == second.candidates[0].candidate_id
    records = [
        json.loads(line)
        for line in (tmp_path / "registry" / "candidate_evaluations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 2
    assert records[-1]["evaluation"]["ready_for_review"] is False


def test_failing_trace_store_does_not_discard_valid_structured_source(tmp_path: Path) -> None:
    class FailingStore:
        def list_by_run(self, _run_id):
            raise OSError("raw private trace failure")

        def list_by_trace(self, _trace_id):
            raise OSError("raw private trace failure")

    source = tmp_path / "valid-after-trace-failure.json"
    _write_source(
        source,
        {
            "source_ref": "pytest:case4",
            "target_type": "code",
            "target_ref": "assistant_agent.agent.runtime:AgentGraphRuntime",
            "symptom_code": "deterministic_test_regression",
            "summary": "The deterministic runtime regression failed.",
            "severity": "high",
            "attributes": {"module": "assistant_agent.agent.runtime", "symbol": "AgentGraphRuntime"},
        },
    )

    report = run_improvement_lab(
        trace_store=FailingStore(),
        run_ids=["broken"],
        trace_ids=[],
        eval_paths=[],
        test_paths=[source],
        target_type=None,
        skill_id=None,
        repo_root=tmp_path,
        registry_root=tmp_path / "registry",
        persist=False,
        proposal_mode="deterministic",
    )

    assert report.candidates
    assert any(issue.code == "evidence_source_read_failed" for issue in report.issues)


def _write_source(path: Path, record: dict) -> None:
    path.write_text(
        json.dumps({"schema_version": "improvement_source_records_v1", "records": [record]}),
        encoding="utf-8",
    )


def _write_skill(root: Path) -> None:
    path = root / "skills" / "realtime_web_search" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
name: realtime_web_search
description: Look up current information through governed search.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- web_search

## Permissions
- tool:web_search

## Required Inputs
- web_search: query

## When To Use
- User asks for current information.
""",
        encoding="utf-8",
    )
