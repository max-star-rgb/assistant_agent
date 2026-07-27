"""Governed skill validation and execution."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.multi_agent.control_plane_models import AgentAuditEvent
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.tools.models import ToolResult, ToolSpec
from assistant_agent.providers.provider_errors import sanitize_error_message
from assistant_agent.tools.registry import ToolRegistry


logger = logging.getLogger("assistant_agent.skills.runtime")

SkillRunStatus = Literal[
    "succeeded",
    "failed",
    "rejected",
    "validation_failed",
]
SkillAttemptStatus = Literal[
    "succeeded",
    "failed",
    "rejected",
]

_UNSUPPORTED_STEP_KEYS = {"command", "exec", "shell", "http", "browser"}
_SKILL_AUDIT_REDACTION = {
    "raw_payloads_included": False,
    "provider_raw_responses_included": False,
    "step_results_included": False,
    "conversation_history_included": False,
}


class SkillAuditSink(Protocol):
    """Audit sink boundary for skill operator events."""

    def append_audit_event(self, event: AgentAuditEvent) -> None:
        """Store one redacted skill audit event."""


class SkillRetryPolicy(BaseModel):
    """Skill step retry declaration."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=0, ge=0)


class SkillStep(BaseModel):
    """One deterministic skill step backed by a governed tool."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    retry: SkillRetryPolicy = Field(default_factory=SkillRetryPolicy)
    checkpoint: bool = False
    idempotency: Literal["none", "optional", "required"] = "none"


class SkillManifest(BaseModel):
    """Validated skill manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["skill_v1"]
    name: str = Field(min_length=1)
    type: Literal["skill"]
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    steps: list[SkillStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "SkillManifest":
        seen: set[str] = set()
        duplicates: list[str] = []
        for step in self.steps:
            if step.id in seen:
                duplicates.append(step.id)
            seen.add(step.id)
        if duplicates:
            raise ValueError(f"duplicate skill step id: {duplicates[0]}")
        return self


class SkillValidationIssue(BaseModel):
    """Prompt-safe skill validation issue."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    step_id: str | None = None
    tool_name: str | None = None


class SkillValidationResult(BaseModel):
    """Validation result for one skill manifest."""

    accepted: bool
    manifest: SkillManifest | None = None
    issues: list[SkillValidationIssue] = Field(default_factory=list)


class SkillCatalog:
    """Explicitly registered skill manifests for one runtime boundary."""

    def __init__(self, *, registry: ToolRegistry) -> None:
        self.registry = registry
        self._manifests: dict[str, SkillManifest] = {}

    def register(
        self,
        payload: Mapping[str, Any] | SkillManifest,
    ) -> SkillValidationResult:
        """Validate and register a skill manifest by its manifest name."""

        validation = validate_skill_manifest(
            _manifest_payload(payload),
            registry=self.registry,
        )
        if validation.accepted and validation.manifest is not None:
            self._manifests[validation.manifest.name] = validation.manifest
        return validation

    def get(self, skill_id: str) -> SkillManifest | None:
        """Return a registered skill manifest by id."""

        return self._manifests.get(skill_id)

    def list_skill_ids(self) -> list[str]:
        """Return registered skill ids in registration order."""

        return list(self._manifests)


class SkillAttemptRecord(BaseModel):
    """Prompt-safe record for one skill step attempt."""

    skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    tool_name: str = Field(min_length=1)
    status: SkillAttemptStatus
    retry_count: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    output_ref: str | None = None
    error_summary: str | None = None
    validation_code: str | None = None


class SkillRunResult(BaseModel):
    """Result of executing a skill."""

    success: bool
    status: SkillRunStatus
    skill_id: str = Field(min_length=1)
    attempts: list[SkillAttemptRecord] = Field(default_factory=list)
    step_results: dict[str, ToolResult] = Field(default_factory=dict)
    issues: list[SkillValidationIssue] = Field(default_factory=list)


class SkillRunRecord(BaseModel):
    """Checkpointable skill run record."""

    skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: SkillRunStatus
    attempts: list[SkillAttemptRecord] = Field(default_factory=list)
    step_results: dict[str, ToolResult] = Field(default_factory=dict)
    issues: list[SkillValidationIssue] = Field(default_factory=list)
    completed_step_ids: list[str] = Field(default_factory=list)
    next_step_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SkillRunSummary(BaseModel):
    """Operator-facing skill run summary without raw step outputs."""

    skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: SkillRunStatus
    attempt_count: int = Field(ge=0)
    completed_step_ids: list[str] = Field(default_factory=list)
    next_step_id: str | None = None
    last_error_summary: str | None = None
    issue_codes: list[str] = Field(default_factory=list)
    updated_at: datetime


class InMemorySkillRunStore:
    """Process-local skill run store for deterministic resume tests."""

    def __init__(self) -> None:
        self._records: dict[str, SkillRunRecord] = {}

    def save(self, record: SkillRunRecord) -> SkillRunRecord:
        """Store or replace one skill run record."""

        self._records[record.run_id] = record
        return record

    def get(self, run_id: str) -> SkillRunRecord | None:
        """Return one skill run record by run id."""

        return self._records.get(run_id)

    def list_by_skill(self, skill_id: str) -> list[SkillRunRecord]:
        """Return records for one skill id in insertion order."""

        return [
            record
            for record in self._records.values()
            if record.skill_id == skill_id
        ]

    def save_result(
        self,
        result: SkillRunResult,
        *,
        state: AgentState,
        existing: SkillRunRecord | None = None,
    ) -> SkillRunRecord:
        """Persist a skill run result as a checkpointable record."""

        return self.save(_record_from_result(result, state=state, existing=existing))


class JsonlSkillRunStore:
    """JSONL-backed skill run store for explicit local durability."""

    def __init__(self, path: Path | str = ".local/skill_runs.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, record: SkillRunRecord) -> SkillRunRecord:
        """Append one skill run record snapshot."""

        with self.path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "kind": "skill_run_record",
                        "payload": record.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return record

    def get(self, run_id: str) -> SkillRunRecord | None:
        """Return the latest skill run record by run id."""

        for record in reversed(self._read_records()):
            if record.run_id == run_id:
                return record
        return None

    def list_by_skill(self, skill_id: str) -> list[SkillRunRecord]:
        """Return latest records for one skill id in insertion order."""

        return [
            record
            for record in self._latest_records()
            if record.skill_id == skill_id
        ]

    def save_result(
        self,
        result: SkillRunResult,
        *,
        state: AgentState,
        existing: SkillRunRecord | None = None,
    ) -> SkillRunRecord:
        """Persist a skill run result as a checkpointable record."""

        return self.save(_record_from_result(result, state=state, existing=existing))

    def _latest_records(self) -> list[SkillRunRecord]:
        latest: dict[str, SkillRunRecord] = {}
        for record in self._read_records():
            latest[record.run_id] = record
        return list(latest.values())

    def _read_records(self) -> list[SkillRunRecord]:
        if not self.path.exists():
            return []
        records: list[SkillRunRecord] = []
        with self.path.open("r", encoding="utf-8") as file:
            for lineno, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    envelope = json.loads(stripped)
                    if not isinstance(envelope, dict):
                        continue
                    if envelope.get("kind") != "skill_run_record":
                        continue
                    payload = envelope.get("payload")
                    if isinstance(payload, dict):
                        records.append(SkillRunRecord.model_validate(payload))
                except (json.JSONDecodeError, ValidationError):
                    logger.warning(
                        "Skipping invalid skill run record in %s at line %s",
                        self.path,
                        lineno,
                    )
        return records


class SkillRunQueryService:
    """Prompt-safe skill run query service."""

    def __init__(
        self,
        *,
        store: InMemorySkillRunStore | JsonlSkillRunStore,
        audit_sink: SkillAuditSink | None = None,
    ) -> None:
        self.store = store
        self.audit_sink = audit_sink

    def get_run_summary(self, run_id: str) -> SkillRunSummary | None:
        """Return one prompt-safe skill run summary."""

        record = self.store.get(run_id)
        if record is None:
            _emit_skill_audit(
                self.audit_sink,
                action="summary",
                outcome="not_found",
                skill_id="unknown",
                run_id=run_id,
                detail={"run_id": run_id},
            )
            return None
        summary = _run_summary_from_record(record)
        _emit_skill_audit(
            self.audit_sink,
            action="summary",
            outcome="found",
            skill_id=summary.skill_id,
            run_id=summary.run_id,
            detail=_summary_audit_detail(summary),
        )
        return summary

    def list_run_summaries(self, skill_id: str) -> list[SkillRunSummary]:
        """Return prompt-safe summaries for one skill id."""

        summaries = [
            _run_summary_from_record(record)
            for record in self.store.list_by_skill(skill_id)
        ]
        _emit_skill_audit(
            self.audit_sink,
            action="list_summaries",
            outcome="found" if summaries else "not_found",
            skill_id=skill_id,
            run_id=None,
            detail={"skill_id": skill_id, "count": len(summaries)},
        )
        return summaries


def validate_skill_manifest(
    payload: Mapping[str, Any],
    *,
    registry: ToolRegistry,
) -> SkillValidationResult:
    """Validate a skill manifest against the governed tool registry."""

    issues = _pre_validation_issues(payload)
    try:
        manifest = SkillManifest.model_validate(payload)
    except ValidationError as exc:
        issues.append(
            SkillValidationIssue(
                code="manifest_invalid",
                message=_validation_error_message(exc),
            )
        )
        return SkillValidationResult(
            accepted=False,
            manifest=None,
            issues=issues,
        )

    issues.extend(_policy_validation_issues(manifest, registry=registry))
    return SkillValidationResult(
        accepted=not issues,
        manifest=manifest if not issues else None,
        issues=issues,
    )


class SkillRunner:
    """Execute skill steps through the governed tool boundary."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        validator: ActionValidator | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        self.registry = registry
        self.validator = validator or ActionValidator()
        self.tool_executor = tool_executor or ToolExecutor(registry=registry)
        if self.tool_executor.registry is not registry:
            raise ValueError("SkillRunner and ToolExecutor must use the same registry")

    def run(
        self,
        payload: Mapping[str, Any] | SkillManifest,
        state: AgentState,
        *,
        resume_record: SkillRunRecord | None = None,
    ) -> SkillRunResult:
        manifest = _manifest_from_payload(payload)
        if manifest is None:
            validation = validate_skill_manifest(
                payload if isinstance(payload, Mapping) else payload.model_dump(mode="python"),
                registry=self.registry,
            )
            return SkillRunResult(
                success=False,
                status="validation_failed",
                skill_id=_payload_name(payload),
                issues=validation.issues,
            )
        validation = validate_skill_manifest(
            manifest.model_dump(mode="python"),
            registry=self.registry,
        )
        if not validation.accepted:
            return SkillRunResult(
                success=False,
                status="validation_failed",
                skill_id=manifest.name,
                issues=validation.issues,
            )

        attempts: list[SkillAttemptRecord] = (
            list(resume_record.attempts) if resume_record is not None else []
        )
        step_results: dict[str, ToolResult] = (
            dict(resume_record.step_results) if resume_record is not None else {}
        )
        completed_step_ids = (
            set(resume_record.completed_step_ids) if resume_record is not None else set()
        )
        for step in manifest.steps:
            if _resume_can_skip_step(
                step,
                completed_step_ids=completed_step_ids,
                step_results=step_results,
            ):
                continue
            step_result = self._run_step(
                manifest=manifest,
                step=step,
                state=state,
                attempts=attempts,
                step_results=step_results,
            )
            if step_result is not None:
                return step_result
        return SkillRunResult(
            success=True,
            status="succeeded",
            skill_id=manifest.name,
            attempts=attempts,
            step_results=step_results,
        )

    def _run_step(
        self,
        *,
        manifest: SkillManifest,
        step: SkillStep,
        state: AgentState,
        attempts: list[SkillAttemptRecord],
        step_results: dict[str, ToolResult],
    ) -> SkillRunResult | None:
        tool_spec = self.registry.get_spec(step.tool)
        for attempt_number in range(1, step.retry.max_retries + 2):
            tool_input = _resolve_step_input(
                step.input,
                state=state,
                step_results=step_results,
            )
            tool_input = _bind_step_idempotency(
                tool_input,
                manifest=manifest,
                step=step,
                state=state,
            )
            decision = AssistantDecision(
                type="tool_call",
                tool_name=step.tool,
                tool_input=tool_input,
                step_id=step.id,
                reason=f"skill:{manifest.name}:{step.id}",
            )
            validation = self.validator.validate(
                decision=decision,
                registry=self.registry,
                request=state.request,
                state=state,
            )
            if not validation.accepted:
                attempts.append(
                    _attempt_record(
                        manifest=manifest,
                        state=state,
                        step=step,
                        attempt_number=attempt_number,
                        status="rejected",
                        validation_code=validation.code,
                        error_summary=validation.message,
                        tool_input=tool_input,
                    )
                )
                return SkillRunResult(
                    success=False,
                    status="rejected",
                    skill_id=manifest.name,
                    attempts=attempts,
                    step_results=step_results,
                )

            result = self.tool_executor.run_tool(
                state,
                step.id,
                step.tool,
                tool_input,
                trace_id=state.trace_id,
                node_name="skill_runner",
                validated_input=validation.validated_input,
            )
            status = _attempt_status(result)
            attempts.append(
                _attempt_record(
                    manifest=manifest,
                    state=state,
                    step=step,
                    attempt_number=attempt_number,
                    status=status,
                    result=result,
                    tool_input=tool_input,
                )
            )
            if status == "succeeded":
                step_results[step.id] = result
                return None
            if attempt_number > step.retry.max_retries or not _step_retry_allowed(
                result,
                tool_spec=tool_spec,
            ):
                step_results[step.id] = result
                return SkillRunResult(
                    success=False,
                    status="failed",
                    skill_id=manifest.name,
                    attempts=attempts,
                    step_results=step_results,
                )
        return None


class SkillLauncher:
    """Launch only explicitly registered skill manifests."""

    def __init__(
        self,
        *,
        catalog: SkillCatalog,
        runner: SkillRunner | None = None,
        run_store: InMemorySkillRunStore | JsonlSkillRunStore | None = None,
        audit_sink: SkillAuditSink | None = None,
    ) -> None:
        self.catalog = catalog
        self.runner = runner or SkillRunner(registry=catalog.registry)
        self.run_store = run_store or InMemorySkillRunStore()
        self.audit_sink = audit_sink
        if self.runner.registry is not catalog.registry:
            raise ValueError(
                "SkillLauncher and SkillCatalog must use the same registry"
            )

    def launch(self, skill_id: str, state: AgentState) -> SkillRunResult:
        """Launch a registered skill manifest by id."""

        manifest = self.catalog.get(skill_id)
        if manifest is None:
            safe_skill_id = skill_id or "unknown"
            result = SkillRunResult(
                success=False,
                status="validation_failed",
                skill_id=safe_skill_id,
                issues=[
                    SkillValidationIssue(
                        code="skill_not_registered",
                        message="Skill manifest is not explicitly registered.",
                    )
                ],
            )
            _emit_skill_audit(
                self.audit_sink,
                action="launch",
                outcome=result.status,
                skill_id=result.skill_id,
                run_id=state.run_id,
                user_id=state.user_id,
                session_id=state.session_id,
                trace_id=state.trace_id,
                detail=_result_audit_detail(result),
            )
            return result
        result = self.runner.run(manifest, state)
        self.run_store.save_result(result, state=state)
        _emit_skill_audit(
            self.audit_sink,
            action="launch",
            outcome=_result_audit_outcome(result),
            skill_id=result.skill_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            trace_id=state.trace_id,
            detail=_result_audit_detail(result),
        )
        return result

    def resume(self, run_id: str, state: AgentState) -> SkillRunResult:
        """Resume a skill run from its latest checkpointable record."""

        record = self.run_store.get(run_id)
        if record is None:
            result = SkillRunResult(
                success=False,
                status="validation_failed",
                skill_id="unknown",
                issues=[
                    SkillValidationIssue(
                        code="skill_run_not_found",
                        message="Skill run record was not found.",
                    )
                ],
            )
            _emit_skill_audit(
                self.audit_sink,
                action="resume",
                outcome=result.status,
                skill_id=result.skill_id,
                run_id=run_id,
                user_id=state.user_id,
                session_id=state.session_id,
                trace_id=state.trace_id,
                detail=_result_audit_detail(result),
            )
            return result
        manifest = self.catalog.get(record.skill_id)
        if manifest is None:
            result = SkillRunResult(
                success=False,
                status="validation_failed",
                skill_id=record.skill_id,
                attempts=list(record.attempts),
                step_results=dict(record.step_results),
                issues=[
                    SkillValidationIssue(
                        code="skill_not_registered",
                        message="Skill manifest is not explicitly registered.",
                    )
                ],
            )
            _emit_skill_audit(
                self.audit_sink,
                action="resume",
                outcome=result.status,
                skill_id=result.skill_id,
                run_id=run_id,
                user_id=state.user_id,
                session_id=state.session_id,
                trace_id=state.trace_id,
                detail=_result_audit_detail(result),
            )
            return result
        state.run_id = record.run_id
        result = self.runner.run(manifest, state, resume_record=record)
        self.run_store.save_result(result, state=state, existing=record)
        _emit_skill_audit(
            self.audit_sink,
            action="resume",
            outcome=_result_audit_outcome(result),
            skill_id=result.skill_id,
            run_id=state.run_id,
            user_id=state.user_id,
            session_id=state.session_id,
            trace_id=state.trace_id,
            detail=_result_audit_detail(result),
        )
        return result

    def get_run(self, run_id: str) -> SkillRunRecord | None:
        """Return one skill run record by run id."""

        return self.run_store.get(run_id)


def _pre_validation_issues(payload: Mapping[str, Any]) -> list[SkillValidationIssue]:
    issues: list[SkillValidationIssue] = []
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return issues
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        if _UNSUPPORTED_STEP_KEYS.intersection(raw_step):
            issues.append(
                SkillValidationIssue(
                    code="unsupported_step_action",
                    message="Skill steps may only declare governed tool calls.",
                    step_id=str(raw_step.get("id") or index),
                )
            )
    return issues


def _policy_validation_issues(
    manifest: SkillManifest,
    *,
    registry: ToolRegistry,
) -> list[SkillValidationIssue]:
    issues: list[SkillValidationIssue] = []
    permissions = set(manifest.permissions)
    for permission in manifest.permissions:
        if not permission.startswith("tool:"):
            issues.append(
                SkillValidationIssue(
                    code="invalid_permission",
                    message="Skill permissions must use tool:<name> vocabulary.",
                )
            )
    for step in manifest.steps:
        if step.tool not in registry.list():
            issues.append(
                SkillValidationIssue(
                    code="unknown_tool",
                    message=f"Unknown skill step tool: {step.tool}.",
                    step_id=step.id,
                    tool_name=step.tool,
                )
            )
            continue
        if permissions and f"tool:{step.tool}" not in permissions:
            issues.append(
                SkillValidationIssue(
                    code="missing_tool_permission",
                    message="Every skill step tool must have a matching tool:<name> permission.",
                    step_id=step.id,
                    tool_name=step.tool,
                )
            )
        tool_spec = registry.get_spec(step.tool)
        if (
            step.retry.max_retries > 0
            and not _tool_replay_safe(tool_spec, step=step)
        ):
            issues.append(
                SkillValidationIssue(
                    code="step_retry_requires_idempotency",
                    message="Retrying a mutating skill step requires idempotency=required.",
                    step_id=step.id,
                    tool_name=step.tool,
                )
            )
    return issues


def _tool_replay_safe(tool_spec: ToolSpec, *, step: SkillStep) -> bool:
    if tool_spec.category == "read":
        return True
    return step.idempotency == "required"


def _resume_can_skip_step(
    step: SkillStep,
    *,
    completed_step_ids: set[str],
    step_results: dict[str, ToolResult],
) -> bool:
    return step.checkpoint and step.id in completed_step_ids and step.id in step_results


def _record_from_result(
    result: SkillRunResult,
    *,
    state: AgentState,
    existing: SkillRunRecord | None = None,
) -> SkillRunRecord:
    created_at = existing.created_at if existing is not None else datetime.now(timezone.utc)
    return SkillRunRecord(
        skill_id=result.skill_id,
        run_id=state.run_id,
        status=result.status,
        attempts=list(result.attempts),
        step_results=dict(result.step_results),
        issues=list(result.issues),
        completed_step_ids=_completed_step_ids(result.attempts),
        next_step_id=_next_step_id(result),
        created_at=created_at,
        updated_at=datetime.now(timezone.utc),
    )


def _completed_step_ids(attempts: list[SkillAttemptRecord]) -> list[str]:
    completed: list[str] = []
    for attempt in attempts:
        if attempt.status == "succeeded" and attempt.step_id not in completed:
            completed.append(attempt.step_id)
    return completed


def _run_summary_from_record(record: SkillRunRecord) -> SkillRunSummary:
    return SkillRunSummary(
        skill_id=record.skill_id,
        run_id=record.run_id,
        status=record.status,
        attempt_count=len(record.attempts),
        completed_step_ids=list(record.completed_step_ids),
        next_step_id=record.next_step_id,
        last_error_summary=_last_error_summary(record.attempts),
        issue_codes=[issue.code for issue in record.issues],
        updated_at=record.updated_at,
    )


def _summary_audit_detail(summary: SkillRunSummary) -> dict[str, Any]:
    return {
        "skill_id": summary.skill_id,
        "status": summary.status,
        "attempt_count": summary.attempt_count,
        "completed_step_ids": list(summary.completed_step_ids),
        "next_step_id": summary.next_step_id,
        "issue_codes": list(summary.issue_codes),
        "has_error": summary.last_error_summary is not None,
    }


def _result_audit_detail(result: SkillRunResult) -> dict[str, Any]:
    return {
        "skill_id": result.skill_id,
        "status": result.status,
        "attempt_count": len(result.attempts),
        "completed_step_ids": _completed_step_ids(result.attempts),
        "next_step_id": _next_step_id(result),
        "issue_codes": [issue.code for issue in result.issues],
        "has_error": _last_error_summary(result.attempts) is not None,
    }


def _result_audit_outcome(result: SkillRunResult) -> str:
    if result.status == "succeeded":
        return "succeeded"
    return result.status


def _emit_skill_audit(
    audit_sink: SkillAuditSink | None,
    *,
    action: str,
    outcome: str,
    skill_id: str,
    run_id: str | None,
    detail: dict[str, Any],
    user_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    if audit_sink is None:
        return
    try:
        audit_sink.append_audit_event(
            AgentAuditEvent(
                event_type=(
                    "skill_query"
                    if action in {"summary", "list_summaries"}
                    else "skill_run"
                ),
                component="skill",
                action=action,
                outcome=outcome,
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                trace_id=trace_id,
                detail=dict(detail),
                redaction=dict(_SKILL_AUDIT_REDACTION),
            )
        )
    except Exception as exc:  # pragma: no cover - defensive audit boundary
        logger.warning("Skill audit event was not recorded: %s", exc)


def _last_error_summary(attempts: list[SkillAttemptRecord]) -> str | None:
    for attempt in reversed(attempts):
        if attempt.error_summary:
            return attempt.error_summary
    return None


def _next_step_id(result: SkillRunResult) -> str | None:
    if result.status in {"failed", "rejected"}:
        for attempt in reversed(result.attempts):
            if attempt.status in {"failed", "rejected"}:
                return attempt.step_id
    return None


def _manifest_from_payload(payload: Mapping[str, Any] | SkillManifest) -> SkillManifest | None:
    if isinstance(payload, SkillManifest):
        return payload
    try:
        return SkillManifest.model_validate(payload)
    except ValidationError:
        return None


def _manifest_payload(payload: Mapping[str, Any] | SkillManifest) -> Mapping[str, Any]:
    if isinstance(payload, SkillManifest):
        return payload.model_dump(mode="python")
    return payload


def _payload_name(payload: Mapping[str, Any] | SkillManifest) -> str:
    if isinstance(payload, SkillManifest):
        return payload.name
    name = payload.get("name") if isinstance(payload, Mapping) else None
    return name if isinstance(name, str) and name else "unknown"


def _resolve_step_input(
    value: dict[str, Any],
    *,
    state: AgentState,
    step_results: dict[str, ToolResult],
) -> dict[str, Any]:
    return {
        key: _resolve_value(item, state=state, step_results=step_results)
        for key, item in value.items()
    }


def _resolve_value(
    value: Any,
    *,
    state: AgentState,
    step_results: dict[str, ToolResult],
) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_value(item, state=state, step_results=step_results)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_value(item, state=state, step_results=step_results)
            for item in value
        ]
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped == "{{ user.request }}":
        return state.request.text or ""
    if stripped.startswith("{{ steps.") and stripped.endswith(" }}"):
        return _resolve_step_reference(stripped[3:-3].strip(), step_results=step_results)
    return value


def _resolve_step_reference(expression: str, *, step_results: dict[str, ToolResult]) -> Any:
    parts = expression.split(".")
    if len(parts) < 3 or parts[0] != "steps":
        return ""
    step_id = parts[1]
    field_path = parts[2:]
    result = step_results.get(step_id)
    if result is None:
        return ""
    current: Any
    if field_path[0] == "output_ref":
        return result.output_ref or ""
    if field_path[0] == "data":
        current = result.data or {}
        field_path = field_path[1:]
    else:
        current = result.data or {}
    for field in field_path:
        if isinstance(current, Mapping):
            current = current.get(field)
        else:
            return ""
    return current if current is not None else ""


def _bind_step_idempotency(
    tool_input: dict[str, Any],
    *,
    manifest: SkillManifest,
    step: SkillStep,
    state: AgentState,
) -> dict[str, Any]:
    if step.idempotency != "required" or tool_input.get("idempotency_key"):
        return tool_input
    return {
        **tool_input,
        "idempotency_key": f"skill:{manifest.name}:{state.run_id}:{step.id}",
    }


def _attempt_status(result: ToolResult) -> SkillAttemptStatus:
    if result.success:
        return "succeeded"
    return "failed"


def _attempt_record(
    *,
    manifest: SkillManifest,
    state: AgentState,
    step: SkillStep,
    attempt_number: int,
    status: SkillAttemptStatus,
    tool_input: dict[str, Any],
    result: ToolResult | None = None,
    validation_code: str | None = None,
    error_summary: str | None = None,
) -> SkillAttemptRecord:
    retry_count = 0
    if result is not None and isinstance(result.trace_summary, dict):
        raw_retry_count = result.trace_summary.get("retry_count")
        if isinstance(raw_retry_count, int) and raw_retry_count >= 0:
            retry_count = raw_retry_count
    return SkillAttemptRecord(
        skill_id=manifest.name,
        run_id=state.run_id,
        step_id=step.id,
        attempt=attempt_number,
        tool_name=step.tool,
        status=status,
        retry_count=retry_count,
        idempotency_key=_string_value(tool_input.get("idempotency_key")),
        output_ref=result.output_ref if result is not None else None,
        error_summary=(
            sanitize_error_message(error_summary)
            if error_summary is not None
            else _result_error_summary(result)
        ),
        validation_code=validation_code,
    )


def _result_error_summary(result: ToolResult | None) -> str | None:
    if result is None or not result.error:
        return None
    return sanitize_error_message(result.error)


def _step_retry_allowed(result: ToolResult, *, tool_spec: ToolSpec) -> bool:
    if result.success:
        return False
    if tool_spec.category != "read" and result.data and result.data.get("status") == "unknown_after_timeout":
        return False
    return True


def _validation_error_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {"msg": "invalid manifest"}
    message = first.get("msg")
    return str(message or "invalid manifest")


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
