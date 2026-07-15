from pathlib import Path
from collections import defaultdict

import pytest

from assistant_agent.memory.framework.collector import (
    BakeoffCase,
    BakeoffCaseEvidence,
    BakeoffCollectionAborted,
    BakeoffCollectionMeasurements,
    BakeoffFrameworkCollector,
    BakeoffLifecycleController,
    BakeoffMemory,
    BakeoffProbeEvidence,
    build_metrics,
    load_bakeoff_cases,
    percentile_95,
    write_evidence,
)
from assistant_agent.schemas.memory_framework import (
    FrameworkHealthResult,
    FrameworkMemoryRecord,
    FrameworkRecallResult,
    FrameworkRetainResult,
)
from assistant_agent.schemas.identity import RequestIdentity


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bakeoff_collector_module_exists() -> None:
    assert (REPO_ROOT / "src/assistant_agent/memory/framework/collector.py").is_file()


def test_fixed_full_corpus_has_50_unique_cases_and_required_categories() -> None:
    cases = load_bakeoff_cases("full")

    assert len(cases) == 50
    assert len({case.case_id for case in cases}) == 50
    assert {case.category for case in cases} == {
        "basic_recall",
        "contradiction",
        "temporal",
        "multihop",
        "chinese",
        "episodic_procedural",
        "write_precision",
        "false_positive",
    }
    assert all(case.memories or case.category == "false_positive" for case in cases)


def test_smoke_corpus_is_fixed_subset_covering_required_quality_checks() -> None:
    cases = load_bakeoff_cases("smoke")

    assert [case.case_id for case in cases] == [
        "basic-001",
        "chinese-001",
        "write-precision-001",
        "false-positive-001",
    ]


@pytest.mark.parametrize(
    ("values", "expected"),
    [([], 0.0), ([10.0], 10.0), ([1.0, 2.0, 3.0, 4.0, 100.0], 100.0)],
)
def test_percentile_95_uses_deterministic_nearest_rank(values, expected) -> None:
    assert percentile_95(values) == expected


def test_metrics_aggregate_quality_isolation_latency_and_hard_gate_probes() -> None:
    case_evidence = [
        BakeoffCaseEvidence(
            case_id="basic-001",
            category="basic_recall",
            expected_ids=["m1"],
            matched_ids=["m1", "noise"],
            retain_latencies_ms=[10.0],
            recall_latency_ms=20.0,
            passed=True,
        ),
        BakeoffCaseEvidence(
            case_id="false-positive-001",
            category="false_positive",
            expected_ids=[],
            matched_ids=[],
            retain_latencies_ms=[],
            recall_latency_ms=30.0,
            passed=True,
        ),
    ]
    probes = [
        BakeoffProbeEvidence(probe_id="cross-user-isolation", passed=True, attempts=4, failures=0),
        BakeoffProbeEvidence(probe_id="crud-history", passed=True),
        BakeoffProbeEvidence(probe_id="export-delete-clear", passed=True),
        BakeoffProbeEvidence(probe_id="audit-mapping", passed=True),
        BakeoffProbeEvidence(probe_id="langgraph-context", passed=True),
        BakeoffProbeEvidence(probe_id="governance-boundary", passed=True),
        BakeoffProbeEvidence(probe_id="restart-recovery", passed=True),
        BakeoffProbeEvidence(probe_id="no-silent-write-loss", passed=True),
    ]

    metrics = build_metrics(
        framework="hindsight",
        cases=case_evidence,
        probes=probes,
        measurements=BakeoffCollectionMeasurements(
            rss_mb=512,
            disk_mb=128,
            cold_start_seconds=4.5,
            backup_portable=True,
            configuration_steps=6,
        ),
    )

    assert metrics.version == "0.8.4"
    assert metrics.recall_at_5 == 1.0
    assert metrics.mrr == 1.0
    assert metrics.false_positive_rate == 0.0
    assert metrics.cross_user_leakage_rate == 0.0
    assert metrics.p95_retain_ms == 10.0
    assert metrics.p95_recall_ms == 30.0
    assert metrics.restart_recovery_verified is True
    assert metrics.no_silent_write_loss is True


def test_metrics_fail_closed_when_isolation_probe_is_missing() -> None:
    metrics = build_metrics(
        framework="mem0",
        cases=[],
        probes=[],
        measurements=BakeoffCollectionMeasurements(),
    )

    assert metrics.cross_user_leakage_rate == 1.0
    assert metrics.governance_bypass_detected is True


def test_evidence_writer_is_deterministic_and_rejects_sensitive_fields(tmp_path) -> None:
    case = BakeoffCaseEvidence(
        case_id="basic-001",
        category="basic_recall",
        expected_ids=["m1"],
        matched_ids=["m1"],
        retain_latencies_ms=[1.25],
        recall_latency_ms=2.5,
        passed=True,
    )
    path = tmp_path / "evidence.json"
    payload = {
        "schema_version": 1,
        "framework": "hindsight",
        "version": "0.8.4",
        "phase": "smoke",
        "cases": [case.model_dump(mode="json")],
        "probes": [],
    }

    write_evidence(path, payload)
    first = path.read_text(encoding="utf-8")
    write_evidence(path, payload)

    assert path.read_text(encoding="utf-8") == first
    assert "generated_at" not in first
    with pytest.raises(ValueError, match="sensitive evidence field"):
        write_evidence(path, {**payload, "api_key": "secret"})


def test_case_schema_rejects_empty_positive_case() -> None:
    with pytest.raises(ValueError, match="positive case requires memories"):
        BakeoffCase(
            case_id="bad",
            category="basic_recall",
            query="anything",
            expected_ids=["m1"],
            memories=[],
        )


def test_case_schema_keeps_only_anonymous_ids_not_provider_payloads() -> None:
    memory = BakeoffMemory(memory_id="m1", text="stable synthetic fact")
    case = BakeoffCase(
        case_id="basic-test",
        category="basic_recall",
        query="stable fact",
        expected_ids=["m1"],
        memories=[memory],
    )

    assert case.memories[0].memory_id == "m1"
    with pytest.raises(ValueError):
        BakeoffMemory(memory_id="m2", text="token=sk-real-secret")


class FakeFrameworkEngine:
    name = "hindsight"

    def __init__(self) -> None:
        self.available = True
        self.records = defaultdict(list)

    def _scope(self, identity):
        return (identity.user_id, identity.agent_id, identity.run_id)

    def health(self):
        return FrameworkHealthResult(status="ok", version="0.8.4")

    def retain(self, request):
        if not self.available:
            raise ConnectionError("sidecar unavailable")
        engine_id = f"eng-{request.project_memory_id}"
        self.records[self._scope(request.identity)].append(
            FrameworkMemoryRecord(
                engine_id=engine_id,
                project_memory_id=request.project_memory_id,
                text=request.text,
                memory_type=request.memory_type,
                source=request.source,
                created_at=request.created_at,
                relevance=1.0,
            )
        )
        return FrameworkRetainResult(accepted=True, engine_ids=[engine_id])

    def recall(self, request):
        if not self.available:
            raise ConnectionError("sidecar unavailable")
        query_terms = {term.lower().strip("?.,") for term in request.query.split() if len(term) > 2}
        records = [
            record
            for record in reversed(self.records[self._scope(request.identity)])
            if query_terms & {term.lower().strip("?.,") for term in record.text.split()}
        ]
        return FrameworkRecallResult(records=records[: request.top_k], total=len(records))

    def reflect(self, request):
        return {}

    def get(self, *, identity, engine_id):
        record = next(
            (item for item in self.records[self._scope(identity)] if item.engine_id == engine_id),
            None,
        )
        return (
            {
                "id": record.engine_id,
                "text": record.text,
                "metadata": {
                    "project_memory_id": record.project_memory_id,
                    "memory_type": record.memory_type,
                    "source": record.source,
                },
                "created_at": record.created_at.isoformat(),
            }
            if record
            else None
        )

    def list(self, *, identity):
        return [self.get(identity=identity, engine_id=item.engine_id) for item in self.records[self._scope(identity)]]

    def history(self, *, identity, engine_id):
        return []

    def delete(self, *, identity, engine_id, project_memory_id=None):
        before = len(self.records[self._scope(identity)])
        self.records[self._scope(identity)] = [
            item for item in self.records[self._scope(identity)] if item.engine_id != engine_id
        ]
        return len(self.records[self._scope(identity)]) < before

    def clear(self, *, identity):
        count = len(self.records[self._scope(identity)])
        self.records[self._scope(identity)] = []
        return count

    def export(self, *, identity):
        return self.list(identity=identity)


class FakeLifecycle(BakeoffLifecycleController):
    def __init__(self, engine: FakeFrameworkEngine) -> None:
        self.engine = engine

    def restart(self) -> None:
        self.engine.available = True

    def stop(self) -> None:
        self.engine.available = False

    def start(self) -> None:
        self.engine.available = True

    def measurements(self) -> BakeoffCollectionMeasurements:
        return BakeoffCollectionMeasurements(
            rss_mb=64,
            disk_mb=8,
            cold_start_seconds=0.1,
            backup_portable=True,
            configuration_steps=4,
        )


def test_collector_runs_cases_and_governance_probes_through_tool_executor(tmp_path) -> None:
    engine = FakeFrameworkEngine()
    case = BakeoffCase(
        case_id="basic-test",
        category="basic_recall",
        query="Which milk does the user drink?",
        memories=[BakeoffMemory(memory_id="basic-test-m1", text="user drinks oat milk")],
        expected_ids=["basic-test-m1"],
    )
    collector = BakeoffFrameworkCollector(
        framework="hindsight",
        phase="full",
        adapter=engine,
        ledger_path=tmp_path / "ledger.sqlite3",
        lifecycle=FakeLifecycle(engine),
        cases=[case],
    )

    result = collector.collect()

    assert result.metrics.recall_at_5 == 1.0, result.cases[0]
    assert result.cases[0].matched_ids == ["basic-test-m1"]
    assert result.tool_calls > 0
    assert all(probe.error_code is None for probe in result.probes), [
        probe for probe in result.probes if probe.error_code
    ]
    context_events = collector.manager.list_audit_events_for_identity(
        RequestIdentity.for_user(
            tenant_id="probe-tenant",
            user_id="probe-context",
            project_id="probe-project-context",
            session_id="probe-session-context",
        ),
        event_type="memory_context_loaded",
    )
    assert len(context_events) == 1
    serialized = result.model_dump_json()
    assert "user drinks oat milk" not in serialized
    assert "Which milk" not in serialized


def test_isolation_probe_rejects_a_queued_owner_write(tmp_path) -> None:
    class RejectingEngine(FakeFrameworkEngine):
        def retain(self, request):
            return FrameworkRetainResult(accepted=False)

    engine = RejectingEngine()
    collector = BakeoffFrameworkCollector(
        framework="hindsight",
        phase="smoke",
        adapter=engine,
        ledger_path=tmp_path / "ledger.sqlite3",
        lifecycle=FakeLifecycle(engine),
        cases=[
            BakeoffCase(
                case_id="basic-test",
                category="basic_recall",
                query="Which milk?",
                memories=[BakeoffMemory(memory_id="basic-test-m1", text="user drinks oat milk")],
                expected_ids=["basic-test-m1"],
            )
        ],
    )

    assert collector._isolation_probe().passed is False


def test_outbox_probe_recovers_from_one_transient_retry_failure(tmp_path) -> None:
    class TransientRecoveryEngine(FakeFrameworkEngine):
        recovery_attempts = 0

        def retain(self, request):
            if self.available and request.project_memory_id == "outbox-marker":
                self.recovery_attempts += 1
                if self.recovery_attempts == 1:
                    return FrameworkRetainResult(accepted=False)
            return super().retain(request)

    engine = TransientRecoveryEngine()
    collector = BakeoffFrameworkCollector(
        framework="hindsight",
        phase="smoke",
        adapter=engine,
        ledger_path=tmp_path / "ledger.sqlite3",
        lifecycle=FakeLifecycle(engine),
        cases=[
            BakeoffCase(
                case_id="basic-test",
                category="basic_recall",
                query="Which milk?",
                memories=[BakeoffMemory(memory_id="basic-test-m1", text="user drinks oat milk")],
                expected_ids=["basic-test-m1"],
            )
        ],
    )

    assert collector._outbox_probe().passed is True
    assert engine.recovery_attempts == 2


def test_retry_tool_reports_failed_outbox_attempt_as_failure(tmp_path) -> None:
    class RejectingRecoveryEngine(FakeFrameworkEngine):
        def retain(self, request):
            if self.available:
                return FrameworkRetainResult(accepted=False)
            return super().retain(request)

    engine = RejectingRecoveryEngine()
    lifecycle = FakeLifecycle(engine)
    collector = BakeoffFrameworkCollector(
        framework="hindsight",
        phase="smoke",
        adapter=engine,
        ledger_path=tmp_path / "ledger.sqlite3",
        lifecycle=lifecycle,
        cases=[
            BakeoffCase(
                case_id="basic-test",
                category="basic_recall",
                query="Which milk?",
                memories=[BakeoffMemory(memory_id="basic-test-m1", text="user drinks oat milk")],
                expected_ids=["basic-test-m1"],
            )
        ],
    )
    identity = RequestIdentity.for_user(user_id="retry-user", session_id="retry-session")
    lifecycle.stop()
    collector._execute(
        identity,
        operation="retain",
        memory_id="retry-marker",
        text="The stable retry project codename is Orion.",
    )
    lifecycle.start()

    retried, _ = collector._execute(identity, operation="retry")

    assert retried.success is False
    assert retried.error == "memory_bakeoff_outbox_retry_failed"


def test_collectors_do_not_reuse_tool_idempotency_results_across_runs(tmp_path) -> None:
    engine = FakeFrameworkEngine()
    lifecycle = FakeLifecycle(engine)
    case = BakeoffCase(
        case_id="basic-test",
        category="basic_recall",
        query="Which milk?",
        memories=[BakeoffMemory(memory_id="basic-test-m1", text="user drinks oat milk")],
        expected_ids=["basic-test-m1"],
    )
    identity = RequestIdentity.for_user(user_id="repeat-user", session_id="repeat-session")
    execution_nonces = []

    for index in range(2):
        collector = BakeoffFrameworkCollector(
            framework="hindsight",
            phase="smoke",
            adapter=engine,
            ledger_path=tmp_path / f"ledger-{index}.sqlite3",
            lifecycle=lifecycle,
            cases=[case],
        )
        original_run_tool = collector.executor.run_tool

        def capture_run_tool(state, step_id, tool_name, tool_input):
            execution_nonces.append(tool_input.get("execution_nonce"))
            return original_run_tool(state, step_id, tool_name, tool_input)

        collector.executor.run_tool = capture_run_tool
        collector._execute(
            identity,
            operation="retain",
            memory_id="repeat-marker",
            text="The stable repeated project codename is Orion.",
        )

    assert sum(len(records) for records in engine.records.values()) == 2
    assert all(execution_nonces)
    assert len(set(execution_nonces)) == 2


def test_smoke_aborts_with_stable_code_when_retain_cannot_recover(tmp_path) -> None:
    engine = FakeFrameworkEngine()
    engine.available = False

    class BrokenLifecycle(FakeLifecycle):
        def start(self) -> None:
            self.engine.available = False

        def restart(self) -> None:
            self.engine.available = False

    collector = BakeoffFrameworkCollector(
        framework="hindsight",
        phase="smoke",
        adapter=engine,
        ledger_path=tmp_path / "ledger.sqlite3",
        lifecycle=BrokenLifecycle(engine),
        cases=[
            BakeoffCase(
                case_id="basic-test",
                category="basic_recall",
                query="Which milk?",
                memories=[BakeoffMemory(memory_id="basic-test-m1", text="user drinks oat milk")],
                expected_ids=["basic-test-m1"],
            )
        ],
    )

    with pytest.raises(BakeoffCollectionAborted, match="memory_bakeoff_smoke_retain_failed"):
        collector.collect()


def test_collector_rejects_empty_corpus(tmp_path) -> None:
    engine = FakeFrameworkEngine()
    with pytest.raises(ValueError, match="bake-off corpus is empty"):
        BakeoffFrameworkCollector(
            framework="hindsight",
            phase="full",
            adapter=engine,
            ledger_path=tmp_path / "ledger.sqlite3",
            lifecycle=FakeLifecycle(engine),
            cases=[],
        )
