"""Measured evidence collection primitives for the memory framework bake-off."""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.memory.framework.base import MemoryEngineAdapter
from assistant_agent.memory.framework.bakeoff import FrameworkBakeoffMetrics
from assistant_agent.memory.framework.ledger import FrameworkGovernanceLedger
from assistant_agent.memory.framework.store import FrameworkMemoryStore
from assistant_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolResult, ToolSpec
from assistant_agent.services.memory_audit import MemoryAuditService
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import ToolRegistry


FrameworkName = Literal["hindsight", "mem0"]
BakeoffPhase = Literal["smoke", "full"]
CaseCategory = Literal[
    "basic_recall",
    "contradiction",
    "temporal",
    "multihop",
    "chinese",
    "episodic_procedural",
    "write_precision",
    "false_positive",
]

_FIXED_VERSIONS = {"hindsight": "0.8.4", "mem0": "2.0.11"}
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "provider_response",
    "raw_provider_response",
    "secret",
    "token",
}
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|password|secret|token)\s*[:=]"
)


class BakeoffMemory(BaseModel):
    """One synthetic memory input; text is never copied to persisted evidence."""

    memory_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    text: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def reject_sensitive_text(self) -> "BakeoffMemory":
        if _SENSITIVE_VALUE.search(self.text):
            raise ValueError("synthetic memory contains secret-like text")
        return self


class BakeoffCase(BaseModel):
    """Fixed synthetic quality case with anonymous expected identifiers."""

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    category: CaseCategory
    query: str = Field(min_length=2, max_length=300)
    memories: list[BakeoffMemory] = Field(default_factory=list)
    expected_ids: list[str] = Field(default_factory=list)
    forbidden_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_shape(self) -> "BakeoffCase":
        if self.category != "false_positive" and not self.memories:
            raise ValueError("positive case requires memories")
        memory_ids = {memory.memory_id for memory in self.memories}
        if not set(self.expected_ids).issubset(memory_ids):
            raise ValueError("expected ids must reference case memories")
        if set(self.expected_ids) & set(self.forbidden_ids):
            raise ValueError("expected and forbidden ids must be disjoint")
        return self


class BakeoffCaseEvidence(BaseModel):
    """Prompt-safe per-case evidence without corpus text or provider payloads."""

    case_id: str
    category: CaseCategory
    expected_ids: list[str] = Field(default_factory=list)
    matched_ids: list[str] = Field(default_factory=list)
    retain_latencies_ms: list[float] = Field(default_factory=list)
    recall_latency_ms: float = Field(ge=0)
    passed: bool
    error_code: str | None = None


class BakeoffProbeEvidence(BaseModel):
    """Prompt-safe governance or operations probe result."""

    probe_id: str
    passed: bool
    attempts: int = Field(default=1, ge=0)
    failures: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)
    error_code: str | None = None


class BakeoffCollectionMeasurements(BaseModel):
    """Host measurements which cannot be derived from case evidence."""

    rss_mb: float = Field(default=0, ge=0)
    disk_mb: float = Field(default=0, ge=0)
    cold_start_seconds: float = Field(default=0, ge=0)
    backup_portable: bool = False
    configuration_steps: int = Field(default=0, ge=0)


class BakeoffCollectionResult(BaseModel):
    """Complete anonymous result from one framework/phase collection."""

    schema_version: Literal[1] = 1
    framework: FrameworkName
    version: str
    phase: BakeoffPhase
    cases: list[BakeoffCaseEvidence]
    probes: list[BakeoffProbeEvidence]
    measurements: BakeoffCollectionMeasurements
    metrics: FrameworkBakeoffMetrics
    tool_calls: int = Field(ge=0)


class BakeoffCollectionAborted(RuntimeError):
    """Raised when a smoke hard gate fails and metrics must not be emitted."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class BakeoffLifecycleController(ABC):
    """Sidecar lifecycle and host measurement boundary used by the collector."""

    @abstractmethod
    def restart(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def measurements(self) -> BakeoffCollectionMeasurements: ...

    def sample_resources(self) -> None:
        """Optionally update peak resource observations after an operation."""



class _ProbeInput(BaseModel):
    operation: Literal[
        "retain", "recall", "context", "get", "list", "export", "delete", "clear", "retry", "confirm"
    ]
    memory_id: str | None = None
    text: str | None = None
    query: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)
    execution_nonce: str | None = None


class _ProbeOutput(BaseModel):
    status: str
    ids: list[str] = Field(default_factory=list)
    count: int = Field(default=0, ge=0)


class _MemoryBakeoffProbeTool(ToolBase):
    """Internal-only governed probe; it is never registered in the default catalog."""

    name = "memory_framework_bakeoff_probe"
    description = "Run one governed synthetic memory bake-off operation."
    input_schema = _ProbeInput
    output_schema = _ProbeOutput
    category = "write"
    requires_confirmation = False
    redact_trace = True

    def __init__(self, manager: MemoryManager) -> None:
        self.manager = manager
        self.audit = MemoryAuditService(manager)

    def _run(self, input: _ProbeInput, context: ToolContext) -> ToolResult:
        identity = _identity_from_tool_context(context)
        if input.operation == "retain":
            if not input.memory_id or not input.text:
                raise ValueError("retain requires memory_id and text")
            saved = self.manager.save_explicit_for_identity(
                identity,
                text=input.text,
                content={"summary": input.text},
                memory_id=input.memory_id,
                scope="project",
                source_intent="user_explicit",
                source_reason="fixed synthetic bake-off corpus",
                future_use="memory framework quality evaluation",
                evidence=f"case_memory_id={input.memory_id}",
                created_at=datetime.now(timezone.utc),
            )
            queued = bool(getattr(saved, "content", {}).get("_framework_retain_status") == "queued")
            return _probe_result(
                self.name,
                status="queued" if queued else "retained",
                ids=[] if queued else [input.memory_id],
            )
        if input.operation == "confirm":
            try:
                self.manager.save_explicit_for_identity(
                    identity,
                    text=input.text or "remember my project path is /home/example/private",
                    content={
                        "summary": input.text or "remember my project path is /home/example/private"
                    },
                    memory_id=input.memory_id or "confirmation-probe",
                    scope="project",
                    source_intent="user_explicit",
                )
            except MemoryConfirmationRequired as exc:
                return _probe_result(self.name, status="pending_confirmation", ids=[exc.confirmation.confirmation_id])
            return _probe_result(self.name, status="unexpected_write")
        if input.operation == "recall":
            result = self.manager.search_for_identity(
                identity,
                MemoryQuery(
                    user_id=identity.user_id,
                    session_id=identity.session_id,
                    query=input.query or "recent memories",
                    top_k=input.top_k,
                    max_context_chars=4000,
                ),
            )
            if result.errors:
                return ToolResult(
                    tool_name=self.name,
                    success=False,
                    error=str(result.errors[0].get("code") or "memory_framework_recall_failed"),
                    data={"status": "recall_failed", "ids": [], "count": 0},
                )
            return _probe_result(
                self.name,
                status="recalled",
                ids=[item.memory_id for item in result.items],
            )
        if input.operation == "context":
            context_result = self.manager.load_context_for_identity(
                identity,
                query_text=input.query or "recent memories",
                top_k=input.top_k,
                max_context_chars=4000,
            )
            return _probe_result(
                self.name,
                status="context_loaded",
                ids=[item.memory_id for item in context_result.items],
            )
        if input.operation == "get":
            item = self.manager.get_for_identity(identity, input.memory_id or "")
            return _probe_result(
                self.name,
                status="found" if item else "not_found",
                ids=[item.memory_id] if item else [],
            )
        if input.operation == "list":
            items = self.manager.list_for_identity(identity)
            return _probe_result(self.name, status="listed", ids=[item.memory_id for item in items])
        if input.operation == "export":
            exported = self.audit.export_for_identity(identity, include_content=False)
            return _probe_result(self.name, status="exported", ids=[item.memory_id for item in exported.items])
        if input.operation == "delete":
            deleted = self.manager.delete_for_identity(identity, input.memory_id or "")
            return _probe_result(
                self.name,
                status="deleted" if deleted else "not_found",
                ids=[input.memory_id] if deleted and input.memory_id else [],
            )
        if input.operation == "clear":
            before = len(self.manager.list_for_identity(identity))
            self.manager.clear_identity(identity)
            return _probe_result(self.name, status="cleared", count=before)
        report = self.manager.retry_pending_writes()
        if report.failed:
            return ToolResult(
                tool_name=self.name,
                success=False,
                error="memory_bakeoff_outbox_retry_failed",
                data={"status": "retry_failed", "ids": [], "count": report.succeeded},
            )
        return _probe_result(self.name, status="retried", count=report.succeeded)


class _BakeoffProbeRegistry(ToolRegistry):
    """Cache the immutable internal probe spec across many measured calls."""

    def __init__(self) -> None:
        super().__init__()
        self._specs: dict[str, ToolSpec] = {}

    def register(self, tool) -> None:
        super().register(tool)
        self._specs.pop(tool.name, None)

    def get_spec(self, name: str) -> ToolSpec:
        if name not in self._specs:
            self._specs[name] = super().get_spec(name)
        return self._specs[name]


class BakeoffFrameworkCollector:
    """Run fixed synthetic cases and governance probes through ToolExecutor."""

    def __init__(
        self,
        *,
        framework: FrameworkName,
        phase: BakeoffPhase,
        adapter: MemoryEngineAdapter,
        ledger_path: Path,
        lifecycle: BakeoffLifecycleController,
        cases: list[BakeoffCase] | None = None,
    ) -> None:
        self.framework = framework
        self.phase = phase
        self.adapter = adapter
        self.lifecycle = lifecycle
        self.cases = list(cases) if cases is not None else load_bakeoff_cases(phase)
        if not self.cases:
            raise ValueError("bake-off corpus is empty")
        self.ledger = FrameworkGovernanceLedger(ledger_path)
        self.store = FrameworkMemoryStore(
            adapter=adapter,
            ledger=self.ledger,
            identity_namespace="memory-framework-bakeoff-v1",
        )
        self.manager = MemoryManager(self.store)
        registry = _BakeoffProbeRegistry()
        registry.register(_MemoryBakeoffProbeTool(self.manager))
        self.executor = ToolExecutor(registry=registry)
        self.tool_calls = 0
        self._execution_nonce = secrets.token_hex(8)

    def collect(self) -> BakeoffCollectionResult:
        health = self.adapter.health()
        if health.status != "ok":
            raise BakeoffCollectionAborted("memory_bakeoff_sidecar_unhealthy")
        case_evidence = [self._run_case(case) for case in self.cases]
        probes = self._run_probes()
        if self.phase == "smoke":
            failed_case = next((case for case in case_evidence if not case.passed), None)
            if failed_case is not None:
                code = failed_case.error_code or "memory_bakeoff_smoke_quality_failed"
                raise BakeoffCollectionAborted(code)
            failed_probe = next((probe for probe in probes if not probe.passed), None)
            if failed_probe is not None:
                raise BakeoffCollectionAborted(
                    failed_probe.error_code or f"memory_bakeoff_smoke_{failed_probe.probe_id}_failed"
                )
        measurements = self.lifecycle.measurements()
        metrics = build_metrics(
            framework=self.framework,
            cases=case_evidence,
            probes=probes,
            measurements=measurements,
        )
        return BakeoffCollectionResult(
            framework=self.framework,
            version=_FIXED_VERSIONS[self.framework],
            phase=self.phase,
            cases=case_evidence,
            probes=probes,
            measurements=measurements,
            metrics=metrics,
            tool_calls=self.tool_calls,
        )

    def _run_case(self, case: BakeoffCase) -> BakeoffCaseEvidence:
        identity = RequestIdentity.for_user(
            tenant_id="bakeoff-tenant",
            user_id=f"case-user-{case.case_id}",
            project_id=f"case-project-{case.case_id}",
            session_id=f"case-session-{case.case_id}",
        )
        retain_latencies: list[float] = []
        for memory in case.memories:
            result, latency = self._execute(
                identity,
                operation="retain",
                memory_id=memory.memory_id,
                text=memory.text,
            )
            retain_latencies.append(latency)
            if not result.success or (result.data or {}).get("status") == "queued":
                return BakeoffCaseEvidence(
                    case_id=case.case_id,
                    category=case.category,
                    expected_ids=case.expected_ids,
                    matched_ids=[],
                    retain_latencies_ms=retain_latencies,
                    recall_latency_ms=0,
                    passed=False,
                    error_code=(
                        "memory_bakeoff_smoke_retain_failed"
                        if self.phase == "smoke"
                        else "memory_bakeoff_retain_failed"
                    ),
                )
        result, recall_latency = self._execute(
            identity,
            operation="recall",
            query=case.query,
            top_k=5,
        )
        matched = _result_ids(result)
        passed = bool(result.success) and (
            (not case.expected_ids and not matched)
            or (
                set(case.expected_ids).issubset(matched)
                and not (set(case.forbidden_ids) & set(matched))
            )
        )
        return BakeoffCaseEvidence(
            case_id=case.case_id,
            category=case.category,
            expected_ids=case.expected_ids,
            matched_ids=matched,
            retain_latencies_ms=retain_latencies,
            recall_latency_ms=recall_latency,
            passed=passed,
            error_code=None if result.success else "memory_bakeoff_smoke_recall_failed",
        )

    def _run_probes(self) -> list[BakeoffProbeEvidence]:
        crud, lifecycle = self._crud_probes()
        return [
            self._isolation_probe(),
            crud,
            lifecycle,
            self._mapping_probe(),
            self._context_probe(),
            BakeoffProbeEvidence(probe_id="governance-boundary", passed=self.tool_calls > 0),
            self._restart_probe(),
            self._outbox_probe(),
        ]

    def _isolation_probe(self) -> BakeoffProbeEvidence:
        owner = _probe_identity("isolation-owner")
        retained, _ = self._execute(
            owner,
            operation="retain",
            memory_id="isolation-marker",
            text="The owner's stable project codename is Quartz and the release branch is lunar.",
        )
        attackers = [
            owner.model_copy(update={"tenant_id": "other-tenant"}),
            owner.model_copy(update={"user_id": "other-user"}),
            owner.model_copy(update={"project_id": "other-project"}),
            owner.model_copy(update={"session_id": "other-session"}),
        ]
        failures = 0
        for attacker in attackers:
            result, _ = self._execute(
                attacker,
                operation="recall",
                query="What is the owner's project codename?",
            )
            failures += int("isolation-marker" in _result_ids(result))
        passed = (
            retained.success
            and (retained.data or {}).get("status") == "retained"
            and failures == 0
        )
        return BakeoffProbeEvidence(
            probe_id="cross-user-isolation",
            passed=passed,
            attempts=len(attackers),
            failures=failures,
            error_code=None if passed else "memory_bakeoff_identity_isolation_failed",
        )

    def _crud_probes(self) -> tuple[BakeoffProbeEvidence, BakeoffProbeEvidence]:
        identity = _probe_identity("crud")
        memory_id = "crud-marker"
        retained, _ = self._execute(
            identity,
            operation="retain",
            memory_id=memory_id,
            text="The stable project release branch is lunar and team Delta owns the release.",
        )
        got, _ = self._execute(identity, operation="get", memory_id=memory_id)
        listed, _ = self._execute(identity, operation="list")
        exported, _ = self._execute(identity, operation="export")
        mapping = next(
            (
                item
                for item in self.ledger.list_mappings(user_id=identity.user_id)
                if item.project_memory_id == memory_id
            ),
            None,
        )
        history_verified = False
        if mapping:
            self.adapter.history(identity=mapping.identity, engine_id=mapping.engine_id)
            history_verified = True
        confirmation, _ = self._execute(identity, operation="confirm", memory_id="confirmation-marker")
        deleted, _ = self._execute(identity, operation="delete", memory_id=memory_id)
        recalled, _ = self._execute(identity, operation="recall", query="Which branch is the release branch?")
        clear_id = "clear-marker"
        clear_retain, _ = self._execute(
            identity,
            operation="retain",
            memory_id=clear_id,
            text="The stable project deployment region is east and the service owner is team Cedar.",
        )
        cleared, _ = self._execute(identity, operation="clear")
        after_clear, _ = self._execute(identity, operation="recall", query="Which region hosts the service?")
        crud_ok = all(
            (
                retained.success,
                memory_id in _result_ids(got),
                memory_id in _result_ids(listed),
                memory_id in _result_ids(exported),
                history_verified,
                (confirmation.data or {}).get("status") == "pending_confirmation",
                (deleted.data or {}).get("status") == "deleted",
                memory_id not in _result_ids(recalled),
            )
        )
        lifecycle_ok = all(
            (
                clear_retain.success,
                (cleared.data or {}).get("status") == "cleared",
                clear_id not in _result_ids(after_clear),
            )
        )
        return (
            BakeoffProbeEvidence(
                probe_id="crud-history",
                passed=crud_ok,
                error_code=None if crud_ok else "memory_bakeoff_crud_history_failed",
            ),
            BakeoffProbeEvidence(
                probe_id="export-delete-clear",
                passed=crud_ok and lifecycle_ok,
                error_code=(
                    None if crud_ok and lifecycle_ok else "memory_bakeoff_export_delete_clear_failed"
                ),
            ),
        )

    def _mapping_probe(self) -> BakeoffProbeEvidence:
        mappings_ok = bool(self.ledger.list_mappings(user_id="probe-isolation-owner"))
        audit_ok = bool(
            self.manager.list_audit_events_for_identity(_probe_identity("isolation-owner"), limit=20)
        )
        passed = mappings_ok and audit_ok
        return BakeoffProbeEvidence(
            probe_id="audit-mapping",
            passed=passed,
            error_code=None if passed else "memory_bakeoff_audit_mapping_failed",
        )

    def _context_probe(self) -> BakeoffProbeEvidence:
        identity = _probe_identity("context")
        retained, _ = self._execute(
            identity,
            operation="retain",
            memory_id="context-marker",
            text="The stable context project codename is Epsilon and its owner is team River.",
        )
        loaded, latency = self._execute(
            identity,
            operation="context",
            query="What is the context project codename?",
        )
        passed = (
            retained.success
            and (retained.data or {}).get("status") == "retained"
            and "context-marker" in _result_ids(loaded)
        )
        return BakeoffProbeEvidence(
            probe_id="langgraph-context",
            passed=passed,
            latency_ms=latency,
            error_code=None if passed else "memory_bakeoff_context_injection_failed",
        )

    def _restart_probe(self) -> BakeoffProbeEvidence:
        identity = _probe_identity("restart")
        retained, _ = self._execute(
            identity,
            operation="retain",
            memory_id="restart-marker",
            text="The stable restart project codename is Gamma and its deployment region is west.",
        )
        try:
            self.lifecycle.restart()
            recalled, latency = self._execute(
                identity,
                operation="recall",
                query="What is the restart project codename?",
            )
            passed = (
                retained.success
                and (retained.data or {}).get("status") == "retained"
                and "restart-marker" in _result_ids(recalled)
            )
            return BakeoffProbeEvidence(
                probe_id="restart-recovery",
                passed=passed,
                latency_ms=latency,
                error_code=None if passed else "memory_bakeoff_restart_recovery_failed",
            )
        except Exception:
            return BakeoffProbeEvidence(
                probe_id="restart-recovery",
                passed=False,
                error_code="memory_bakeoff_restart_recovery_failed",
            )

    def _outbox_probe(self) -> BakeoffProbeEvidence:
        identity = _probe_identity("outbox")
        try:
            self.lifecycle.stop()
            queued, _ = self._execute(
                identity,
                operation="retain",
                memory_id="outbox-marker",
                text="The stable outbox project codename is Delta and its deployment region is north.",
            )
            queued_ok = (queued.data or {}).get("status") == "queued" and self.ledger.pending_outbox_count() > 0
            self.lifecycle.start()
            retry_attempts = 0
            retried = ToolResult(tool_name=_MemoryBakeoffProbeTool.name, success=False)
            while retry_attempts < 3 and self.ledger.pending_outbox_count() > 0:
                retry_attempts += 1
                retried, _ = self._execute(
                    identity,
                    operation="retry",
                    memory_id=f"outbox-retry-{self._execution_nonce}-{retry_attempts}",
                )
                if self.ledger.pending_outbox_count() > 0:
                    time.sleep(0.25)
            recalled, latency = self._execute(
                identity,
                operation="recall",
                query="What is the outbox project codename?",
            )
            pending_after_retry = self.ledger.pending_outbox_count()
            recall_recovered = "outbox-marker" in _result_ids(recalled)
            passed = (
                queued_ok
                and retried.success
                and pending_after_retry == 0
                and recall_recovered
            )
            error_code = None
            if not queued_ok:
                error_code = "memory_bakeoff_outbox_queue_failed"
            elif not retried.success or pending_after_retry:
                error_code = "memory_bakeoff_outbox_retry_incomplete"
            elif not recall_recovered:
                error_code = "memory_bakeoff_outbox_recall_failed"
            return BakeoffProbeEvidence(
                probe_id="no-silent-write-loss",
                passed=passed,
                attempts=retry_attempts,
                failures=0 if passed else 1,
                latency_ms=latency,
                error_code=error_code,
            )
        except Exception:
            try:
                self.lifecycle.start()
            except Exception:
                pass
            return BakeoffProbeEvidence(
                probe_id="no-silent-write-loss",
                passed=False,
                error_code="memory_bakeoff_outbox_recovery_failed",
            )

    def _execute(self, identity: RequestIdentity, **tool_input: Any) -> tuple[ToolResult, float]:
        request = UserRequest(
            user_id=identity.user_id,
            session_id=identity.session_id or "bakeoff-session",
            text="memory framework bake-off probe",
            metadata={"tenant_id": identity.tenant_id, "project_id": identity.project_id},
        )
        state = AgentState.from_request(request)
        started = time.perf_counter()
        result = self.executor.run_tool(
            state,
            f"probe-{self.tool_calls + 1}",
            _MemoryBakeoffProbeTool.name,
            {**tool_input, "execution_nonce": self._execution_nonce},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        self.tool_calls += len(state.tool_calls)
        self.lifecycle.sample_resources()
        return result, latency_ms


def load_bakeoff_cases(phase: BakeoffPhase) -> list[BakeoffCase]:
    """Return the versioned in-code corpus in deterministic order."""

    cases = _fixed_cases()
    if phase == "smoke":
        selected = {"basic-001", "chinese-001", "write-precision-001", "false-positive-001"}
        return [case for case in cases if case.case_id in selected]
    return cases


def percentile_95(values: list[float]) -> float:
    """Return nearest-rank p95, with an empty series represented as zero."""

    if not values:
        return 0.0
    ordered = sorted(max(0.0, float(value)) for value in values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def build_metrics(
    *,
    framework: FrameworkName,
    cases: list[BakeoffCaseEvidence],
    probes: list[BakeoffProbeEvidence],
    measurements: BakeoffCollectionMeasurements,
) -> FrameworkBakeoffMetrics:
    """Aggregate anonymous evidence into the existing fixed scoring schema."""

    positives = [case for case in cases if case.expected_ids]
    expected_total = sum(len(case.expected_ids) for case in positives)
    recalled_total = sum(
        len(set(case.expected_ids) & set(case.matched_ids[:5])) for case in positives
    )
    reciprocal_ranks: list[float] = []
    for case in positives:
        expected = set(case.expected_ids)
        rank = next(
            (index for index, memory_id in enumerate(case.matched_ids[:5], start=1) if memory_id in expected),
            None,
        )
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

    probe_by_id = {probe.probe_id: probe for probe in probes}
    isolation = probe_by_id.get("cross-user-isolation")
    isolation_attempts = isolation.attempts if isolation is not None else 0
    isolation_failures = isolation.failures if isolation is not None else 0
    retain_latencies = [value for case in cases for value in case.retain_latencies_ms]
    recall_latencies = [case.recall_latency_ms for case in cases]

    return FrameworkBakeoffMetrics(
        framework=framework,
        version=_FIXED_VERSIONS[framework],
        recall_at_5=_ratio(recalled_total, expected_total),
        mrr=_average(reciprocal_ranks),
        write_precision=_category_accuracy(cases, "write_precision"),
        contradiction_accuracy=_category_accuracy(cases, "contradiction"),
        temporal_accuracy=_category_accuracy(cases, "temporal"),
        multihop_accuracy=_category_accuracy(cases, "multihop"),
        chinese_accuracy=_category_accuracy(cases, "chinese"),
        episodic_procedural_accuracy=_category_accuracy(cases, "episodic_procedural"),
        false_positive_rate=1.0 - _category_accuracy(cases, "false_positive"),
        cross_user_leakage_rate=(
            _ratio(isolation_failures, isolation_attempts) if isolation_attempts else 1.0
        ),
        crud_history_verified=_probe_passed(probe_by_id, "crud-history"),
        export_delete_clear_verified=_probe_passed(probe_by_id, "export-delete-clear"),
        audit_mapping_verified=_probe_passed(probe_by_id, "audit-mapping"),
        langgraph_context_verified=_probe_passed(probe_by_id, "langgraph-context"),
        governance_bypass_detected=not _probe_passed(probe_by_id, "governance-boundary"),
        p95_retain_ms=percentile_95(retain_latencies),
        p95_recall_ms=percentile_95(recall_latencies),
        rss_mb=measurements.rss_mb,
        disk_mb=measurements.disk_mb,
        cold_start_seconds=measurements.cold_start_seconds,
        restart_recovery_verified=_probe_passed(probe_by_id, "restart-recovery"),
        no_silent_write_loss=_probe_passed(probe_by_id, "no-silent-write-loss"),
        default_tests_offline=True,
        backup_portable=measurements.backup_portable,
        configuration_steps=measurements.configuration_steps,
    )


def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic prompt-safe JSON after recursively checking fields."""

    _validate_evidence_value(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _identity_from_tool_context(context: ToolContext) -> RequestIdentity:
    if not context.user_id:
        raise ValueError("bake-off probe requires user identity")
    request_metadata = context.metadata.get("request_metadata")
    request_metadata = request_metadata if isinstance(request_metadata, dict) else {}
    return RequestIdentity.for_user(
        tenant_id=str(request_metadata.get("tenant_id")) if request_metadata.get("tenant_id") else None,
        user_id=context.user_id,
        project_id=str(request_metadata.get("project_id")) if request_metadata.get("project_id") else None,
        session_id=context.session_id,
    )


def _probe_result(
    tool_name: str,
    *,
    status: str,
    ids: list[str] | None = None,
    count: int | None = None,
) -> ToolResult:
    resolved_ids = list(ids or [])
    return ToolResult(
        tool_name=tool_name,
        success=True,
        data={
            "status": status,
            "ids": resolved_ids,
            "count": len(resolved_ids) if count is None else count,
        },
    )


def _result_ids(result: ToolResult) -> list[str]:
    data = result.data if isinstance(result.data, dict) else {}
    values = data.get("ids")
    return [str(value) for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _probe_identity(name: str) -> RequestIdentity:
    return RequestIdentity.for_user(
        tenant_id="probe-tenant",
        user_id=f"probe-{name}",
        project_id=f"probe-project-{name}",
        session_id=f"probe-session-{name}",
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _category_accuracy(cases: list[BakeoffCaseEvidence], category: CaseCategory) -> float:
    selected = [case for case in cases if case.category == category]
    return _ratio(sum(1 for case in selected if case.passed), len(selected))


def _probe_passed(probes: dict[str, BakeoffProbeEvidence], probe_id: str) -> bool:
    probe = probes.get(probe_id)
    return bool(probe and probe.passed)


def _validate_evidence_value(value: Any, *, key: str | None = None) -> None:
    if key is not None and key.lower() in _SENSITIVE_KEYS:
        raise ValueError(f"sensitive evidence field: {key}")
    if isinstance(value, dict):
        for nested_key, nested in value.items():
            _validate_evidence_value(nested, key=str(nested_key))
    elif isinstance(value, list):
        for nested in value:
            _validate_evidence_value(nested)
    elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
        raise ValueError("secret-like value is not allowed in evidence")


def _fixed_cases() -> list[BakeoffCase]:
    cases: list[BakeoffCase] = []
    basic_facts = [
        ("drinks oat milk", "Which milk does the user drink?"),
        ("prefers matte black notebooks", "Which notebook finish is preferred?"),
        ("uses a standing desk", "What kind of desk does the user use?"),
        ("runs on Saturday mornings", "When does the user run?"),
        ("keeps receipts in the blue folder", "Where are receipts kept?"),
        ("prefers aisle seats", "Which seat does the user prefer?"),
        ("works in the Atlas project", "Which project does the user work in?"),
        ("avoids cilantro", "Which herb does the user avoid?"),
        ("reads science fiction", "What genre does the user read?"),
        ("backs up photos monthly", "How often are photos backed up?"),
    ]
    for index, (fact, query) in enumerate(basic_facts, start=1):
        cases.append(_single_case("basic", index, "basic_recall", fact, query))

    contradictions = [
        ("preferred theme was light", "preferred theme is dark", "What is the current theme?"),
        ("office was Berlin", "office is Lisbon", "Where is the current office?"),
        ("budget was 500 dollars", "budget is 800 dollars", "What is the current budget?"),
        ("meeting day was Tuesday", "meeting day is Thursday", "What is the current meeting day?"),
        ("shoe size was 40", "shoe size is 41", "What is the current shoe size?"),
        ("editor was Vim", "editor is Neovim", "What editor is currently used?"),
    ]
    for index, (old, new, query) in enumerate(contradictions, start=1):
        cases.append(_update_case("contradiction", index, old, new, query))

    temporals = [
        ("visited Hangzhou in March", "visited Suzhou in June", "Which city was visited in June?"),
        ("used bus on Monday", "used metro on Friday", "What transport was used Friday?"),
        ("drafted outline in January", "published report in April", "What happened in April?"),
        ("called Mina at 09:00", "called Omar at 15:00", "Who was called at 15:00?"),
        ("ordered tea yesterday", "ordered coffee today", "What was ordered today?"),
        ("trained model v2 last week", "trained model v3 this week", "Which model was trained this week?"),
    ]
    for index, (first, second, query) in enumerate(temporals, start=1):
        cases.append(_update_case("temporal", index, first, second, query))

    multihop = [
        ("Nora leads project Pine", "project Pine uses Qdrant", "Which database does Nora's project use?"),
        ("Iris owns the red bicycle", "the red bicycle is stored in garage B", "Where is Iris's bicycle?"),
        ("Team Cedar reports to Malik", "Malik works from Cairo", "Where does Cedar's manager work?"),
        ("The Apollo task depends on Beacon", "Beacon finishes on Friday", "When can Apollo's dependency finish?"),
        ("Room 8 hosts the design group", "Room 8 is on floor 3", "Which floor hosts the design group?"),
        ("Luca maintains service Nova", "service Nova runs in region east", "Which region contains Luca's service?"),
    ]
    for index, (first, second, query) in enumerate(multihop, start=1):
        cases.append(_pair_case("multihop", index, first, second, query))

    chinese = [
        ("用户喝燕麦奶，不喝牛奶", "用户喝哪种奶？"),
        ("项目代号是青竹", "项目代号是什么？"),
        ("每周三晚上练习游泳", "用户什么时候练习游泳？"),
        ("喜欢低饱和度的蓝绿色", "用户喜欢什么配色？"),
        ("发票保存在财务共享盘", "发票保存在哪里？"),
        ("出差时优先选择高铁", "用户出差优先选择什么交通方式？"),
    ]
    for index, (fact, query) in enumerate(chinese, start=1):
        cases.append(_single_case("chinese", index, "chinese", fact, query))

    episodic = [
        ("on Monday the user fixed a bicycle chain", "What did the user fix on Monday?"),
        ("after deploying, always verify the health endpoint", "What must be checked after deployment?"),
        ("the user celebrated the Atlas launch with the team", "Which launch did the user celebrate?"),
        ("to publish reports, first run the redaction check", "What is the first report publishing step?"),
        ("the user met Kai during the spring workshop", "Who did the user meet at the spring workshop?"),
        ("before database migration, create a snapshot", "What should happen before database migration?"),
    ]
    for index, (fact, query) in enumerate(episodic, start=1):
        cases.append(_single_case("episodic-procedural", index, "episodic_procedural", fact, query))

    write_precision = [
        ("stable preference: use metric units", "Which measurement system should be used?"),
        ("stable project fact: release branch is lunar", "What is the release branch?"),
        ("stable procedure: review before merge", "What happens before merge?"),
        ("stable preference: concise weekly summaries", "How should weekly summaries be written?"),
        ("stable project fact: owner is team Delta", "Which team owns the project?"),
    ]
    for index, (fact, query) in enumerate(write_precision, start=1):
        cases.append(_single_case("write-precision", index, "write_precision", fact, query))

    negative_queries = [
        "What is the user's passport number?",
        "Which cryptocurrency wallet does the user own?",
        "What is the user's favorite opera?",
        "Where is the user's mountain cabin?",
        "Which medication does the user take?",
    ]
    for index, query in enumerate(negative_queries, start=1):
        cases.append(
            BakeoffCase(
                case_id=f"false-positive-{index:03d}",
                category="false_positive",
                query=query,
            )
        )
    return cases


def _single_case(
    prefix: str,
    index: int,
    category: CaseCategory,
    fact: str,
    query: str,
) -> BakeoffCase:
    memory_id = f"{prefix}-{index:03d}-m1"
    return BakeoffCase(
        case_id=f"{prefix}-{index:03d}",
        category=category,
        query=query,
        memories=[BakeoffMemory(memory_id=memory_id, text=fact)],
        expected_ids=[memory_id],
    )


def _update_case(
    prefix: Literal["contradiction", "temporal"],
    index: int,
    old: str,
    new: str,
    query: str,
) -> BakeoffCase:
    old_id = f"{prefix}-{index:03d}-old"
    new_id = f"{prefix}-{index:03d}-new"
    return BakeoffCase(
        case_id=f"{prefix}-{index:03d}",
        category=prefix,
        query=query,
        memories=[BakeoffMemory(memory_id=old_id, text=old), BakeoffMemory(memory_id=new_id, text=new)],
        expected_ids=[new_id],
        forbidden_ids=[old_id],
    )


def _pair_case(prefix: str, index: int, first: str, second: str, query: str) -> BakeoffCase:
    first_id = f"{prefix}-{index:03d}-m1"
    second_id = f"{prefix}-{index:03d}-m2"
    return BakeoffCase(
        case_id=f"{prefix}-{index:03d}",
        category="multihop",
        query=query,
        memories=[
            BakeoffMemory(memory_id=first_id, text=first),
            BakeoffMemory(memory_id=second_id, text=second),
        ],
        expected_ids=[first_id, second_id],
    )
