"""Offline memory retrieval eval helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from pydantic import BaseModel, Field

from assistant_agent.memory.manager import MemoryConfirmationRequired, MemoryManager
from assistant_agent.memory.sqlite_store import SQLiteMemoryStore
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery, MemoryScope, MemorySensitivity, MemoryType


class MemoryRetrievalEvalFixture(BaseModel):
    """One memory item fixture used by a retrieval eval case."""

    memory_id: str
    user_id: str = "u1"
    tenant_id: str | None = None
    project_id: str | None = None
    session_id: str | None = "s1"
    scope: MemoryScope | None = None
    memory_type: MemoryType = "task"
    summary: str
    content: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    source: str = "memory_retrieval_eval"
    sensitivity: MemorySensitivity = "normal"
    created_at: datetime = Field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    expires_at: datetime | None = None

    def to_memory_item(self) -> MemoryItem:
        return MemoryItem(
            memory_id=self.memory_id,
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            project_id=self.project_id,
            session_id=self.session_id,
            scope=self.scope,
            memory_type=self.memory_type,
            summary=self.summary,
            content=self.content,
            tags=self.tags,
            artifact_refs=self.artifact_refs,
            source=self.source,
            sensitivity=self.sensitivity,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )


class MemoryRetrievalExplicitSave(BaseModel):
    """One explicit save operation used to build realistic eval state."""

    text: str
    content: dict[str, Any] = Field(default_factory=dict)
    memory_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    project_id: str | None = None
    session_id: str | None = None
    scope: MemoryScope | None = None
    created_at: datetime = Field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))


class MemoryRetrievalEvalCase(BaseModel):
    """A deterministic offline memory retrieval and injection eval case."""

    id: str
    backend: Literal["memory", "sqlite"] = "memory"
    query: str
    user_id: str = "u1"
    tenant_id: str | None = None
    project_id: str | None = None
    session_id: str | None = "s1"
    allowed_scopes: list[str] | None = None
    capability: str | None = None
    memory_types: list[MemoryType] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=50)
    max_context_chars: int = Field(default=500, ge=50, le=4000)
    max_context_tokens: int | None = Field(default=None, ge=1)
    fixtures: list[MemoryRetrievalEvalFixture] = Field(default_factory=list)
    explicit_saves: list[MemoryRetrievalExplicitSave] = Field(default_factory=list)
    expected_memory_ids: list[str] = Field(default_factory=list)
    expected_injected_ids: list[str] | None = None
    expected_profile_source_memory_ids: list[str] | None = None
    expected_profile_conflict_count: int | None = None
    expected_confirmation_count: int = Field(default=0, ge=0)
    expected_empty: bool = False
    forbidden_memory_ids: list[str] = Field(default_factory=list)
    forbidden_injected_ids: list[str] = Field(default_factory=list)
    forbidden_profile_source_memory_ids: list[str] = Field(default_factory=list)


class MemoryRetrievalEvalResult(BaseModel):
    """Result and metrics for one memory retrieval eval case."""

    id: str
    backend: Literal["memory", "sqlite"] = "memory"
    passed: bool
    query: str
    expected_memory_ids: list[str]
    retrieved_memory_ids: list[str]
    expected_injected_ids: list[str] | None = None
    injected_memory_ids: list[str]
    expected_profile_source_memory_ids: list[str] | None = None
    profile_source_memory_ids: list[str] = Field(default_factory=list)
    profile_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    confirmation_count: int = Field(default=0, ge=0)
    missing_expected_ids: list[str] = Field(default_factory=list)
    forbidden_retrieved_ids: list[str] = Field(default_factory=list)
    forbidden_injected_ids: list[str] = Field(default_factory=list)
    forbidden_profile_source_ids: list[str] = Field(default_factory=list)
    recall_at_k: float = Field(ge=0.0, le=1.0)
    reciprocal_rank: float = Field(ge=0.0, le=1.0)
    expected_empty: bool = False
    empty_correct: bool = False
    false_positive: bool = False
    sensitive_injected: bool = False
    expired_injected: bool = False
    token_budget_compliant: bool = True
    memory_tokens: int = Field(default=0, ge=0)
    memory_token_budget: int = Field(default=0, ge=0)
    omitted_count: int = Field(default=0, ge=0)
    rejected_reasons: list[str] = Field(default_factory=list)


def evaluate_memory_retrieval_case(payload: dict[str, Any]) -> MemoryRetrievalEvalResult:
    """Evaluate one memory retrieval/injection case without external services."""

    case = MemoryRetrievalEvalCase.model_validate(payload)
    temporary_directory: TemporaryDirectory[str] | None = None
    if case.backend == "sqlite":
        temporary_directory = TemporaryDirectory(prefix="assistant-agent-memory-eval-")
        store = SQLiteMemoryStore(
            Path(temporary_directory.name) / "memory.sqlite3",
            synchronous="OFF",
            busy_timeout_ms=1000,
        )
    else:
        store = InMemoryStore()
    manager = MemoryManager(store)
    for fixture in case.fixtures:
        store.save(fixture.to_memory_item())
    confirmation_count = 0
    for explicit_save in case.explicit_saves:
        save_identity = RequestIdentity.for_user(
            tenant_id=explicit_save.tenant_id or case.tenant_id,
            user_id=explicit_save.user_id or case.user_id,
            project_id=explicit_save.project_id or case.project_id,
            session_id=explicit_save.session_id or case.session_id,
            allowed_scopes=case.allowed_scopes,
        )
        try:
            manager.save_explicit_for_identity(
                save_identity,
                text=explicit_save.text,
                content=explicit_save.content,
                memory_id=explicit_save.memory_id,
                scope=explicit_save.scope,
                session_id=explicit_save.session_id or case.session_id,
                created_at=explicit_save.created_at,
            )
        except MemoryConfirmationRequired:
            confirmation_count += 1

    identity = RequestIdentity.for_user(
        tenant_id=case.tenant_id,
        user_id=case.user_id,
        project_id=case.project_id,
        session_id=case.session_id,
        allowed_scopes=case.allowed_scopes,
    )
    query = MemoryQuery(
        user_id=case.user_id,
        tenant_id=case.tenant_id,
        project_id=case.project_id,
        session_id=case.session_id,
        query=case.query,
        capability=case.capability,
        memory_types=case.memory_types,
        allowed_scopes=case.allowed_scopes or [],
        top_k=case.top_k,
        max_context_chars=case.max_context_chars,
    )
    result = manager.search_for_identity(identity, query)
    context = manager.build_context(
        result.items,
        max_chars=case.max_context_chars,
        max_tokens=case.max_context_tokens,
    )
    profile_status = manager.rebuild_user_profile_for_identity(
        identity,
        dry_run=True,
        record_event=False,
    )

    retrieved_ids = [item.memory_id for item in result.items]
    injected_ids = [item.memory_id for item in context.items]
    profile_source_ids = list(profile_status.expected_source_memory_ids)
    expected_ids = list(case.expected_memory_ids)
    missing_expected = [memory_id for memory_id in expected_ids if memory_id not in retrieved_ids]
    missing_profile_expected = (
        []
        if case.expected_profile_source_memory_ids is None
        else [
            memory_id
            for memory_id in case.expected_profile_source_memory_ids
            if memory_id not in profile_source_ids
        ]
    )
    forbidden_retrieved = [memory_id for memory_id in case.forbidden_memory_ids if memory_id in retrieved_ids]
    forbidden_injected = [memory_id for memory_id in case.forbidden_injected_ids if memory_id in injected_ids]
    forbidden_profile_source = [
        memory_id
        for memory_id in case.forbidden_profile_source_memory_ids
        if memory_id in profile_source_ids
    ]
    expected_injected_missing = (
        []
        if case.expected_injected_ids is None
        else [memory_id for memory_id in case.expected_injected_ids if memory_id not in injected_ids]
    )
    profile_conflict_count_ok = (
        case.expected_profile_conflict_count is None
        or len(profile_status.profile_conflicts) == case.expected_profile_conflict_count
    )
    empty_correct = case.expected_empty and not retrieved_ids
    false_positive = case.expected_empty and bool(retrieved_ids)
    sensitive_injected = any(item.sensitivity == "sensitive" for item in context.items)
    expired_injected = any(_is_expired(item) for item in context.items)
    token_budget = case.max_context_tokens or 0
    token_budget_compliant = token_budget <= 0 or context.total_tokens <= token_budget
    recall_at_k = _recall_at_k(expected_ids, retrieved_ids, expected_empty=case.expected_empty)
    reciprocal_rank = _reciprocal_rank(expected_ids, retrieved_ids, expected_empty=case.expected_empty)
    expected_retrieval_ok = empty_correct if case.expected_empty else not missing_expected
    passed = (
        expected_retrieval_ok
        and not forbidden_retrieved
        and not forbidden_injected
        and not forbidden_profile_source
        and not missing_profile_expected
        and profile_conflict_count_ok
        and confirmation_count == case.expected_confirmation_count
        and not expected_injected_missing
        and not sensitive_injected
        and not expired_injected
        and token_budget_compliant
    )

    return MemoryRetrievalEvalResult(
        id=case.id,
        backend=case.backend,
        passed=passed,
        query=case.query,
        expected_memory_ids=expected_ids,
        retrieved_memory_ids=retrieved_ids,
        expected_injected_ids=case.expected_injected_ids,
        injected_memory_ids=injected_ids,
        expected_profile_source_memory_ids=case.expected_profile_source_memory_ids,
        profile_source_memory_ids=profile_source_ids,
        profile_conflicts=profile_status.profile_conflicts,
        confirmation_count=confirmation_count,
        missing_expected_ids=[*missing_expected, *expected_injected_missing, *missing_profile_expected],
        forbidden_retrieved_ids=forbidden_retrieved,
        forbidden_injected_ids=forbidden_injected,
        forbidden_profile_source_ids=forbidden_profile_source,
        recall_at_k=recall_at_k,
        reciprocal_rank=reciprocal_rank,
        expected_empty=case.expected_empty,
        empty_correct=empty_correct,
        false_positive=false_positive,
        sensitive_injected=sensitive_injected,
        expired_injected=expired_injected,
        token_budget_compliant=token_budget_compliant,
        memory_tokens=context.total_tokens,
        memory_token_budget=token_budget,
        omitted_count=context.omitted_count,
        rejected_reasons=context.rejected_reasons,
    )


def summarize_memory_retrieval_eval(results: list[MemoryRetrievalEvalResult]) -> dict[str, Any]:
    """Return aggregate memory retrieval eval metrics."""

    summary = _memory_retrieval_metrics(results)
    summary["by_backend"] = {
        backend: _memory_retrieval_metrics(
            [result for result in results if result.backend == backend]
        )
        for backend in sorted({result.backend for result in results})
    }
    return summary


def _memory_retrieval_metrics(results: list[MemoryRetrievalEvalResult]) -> dict[str, Any]:
    total = len(results)
    empty_results = [result for result in results if result.expected_empty]
    return {
        "total": total,
        "passed": sum(1 for result in results if result.passed),
        "failed": sum(1 for result in results if not result.passed),
        "recall_at_k": _mean([result.recall_at_k for result in results]),
        "mrr": _mean([result.reciprocal_rank for result in results]),
        "false_positive_rate": _rate([result.false_positive for result in empty_results]),
        "correct_empty_rate": _rate([result.empty_correct for result in empty_results]),
        "cross_user_leakage_rate": _rate([bool(result.forbidden_retrieved_ids) for result in results]),
        "sensitive_injection_rate": _rate([result.sensitive_injected for result in results]),
        "expired_injection_rate": _rate([result.expired_injected for result in results]),
        "token_budget_compliance": _rate([result.token_budget_compliant for result in results]),
    }


def summarize_memory_retrieval_eval_dicts(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize serialized retrieval eval results from the generic eval runner."""

    return summarize_memory_retrieval_eval(
        [MemoryRetrievalEvalResult.model_validate(result) for result in results]
    )


def _recall_at_k(expected_ids: list[str], retrieved_ids: list[str], *, expected_empty: bool) -> float:
    if expected_empty:
        return 1.0 if not retrieved_ids else 0.0
    if not expected_ids:
        return 1.0
    hits = len(set(expected_ids).intersection(retrieved_ids))
    return hits / len(set(expected_ids))


def _reciprocal_rank(expected_ids: list[str], retrieved_ids: list[str], *, expected_empty: bool) -> float:
    if expected_empty:
        return 1.0 if not retrieved_ids else 0.0
    expected = set(expected_ids)
    if not expected:
        return 1.0
    for index, memory_id in enumerate(retrieved_ids, start=1):
        if memory_id in expected:
            return 1.0 / index
    return 0.0


def _is_expired(item: MemoryItem) -> bool:
    if item.expires_at is None:
        return False
    now = datetime.now(tz=item.expires_at.tzinfo or timezone.utc)
    return item.expires_at < now


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 1.0


def _rate(values: list[bool]) -> float:
    return sum(1 for value in values if value) / len(values) if values else 0.0
