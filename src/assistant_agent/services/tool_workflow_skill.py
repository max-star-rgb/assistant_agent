"""Governed workflow skill validation and execution."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.services.tool_policy import ToolPolicyInterpreter, ToolPolicyView
from assistant_agent.tools.registry import ToolRegistry


logger = logging.getLogger("assistant_agent.services.tool_workflow_skill")

WorkflowSkillRunStatus = Literal[
    "succeeded",
    "failed",
    "rejected",
    "waiting_confirmation",
    "validation_failed",
]
WorkflowSkillAttemptStatus = Literal[
    "succeeded",
    "failed",
    "rejected",
    "waiting_confirmation",
]

_READ_ONLY_LEVELS = {"none", "local_read", "external_read"}
_UNSUPPORTED_STEP_KEYS = {"command", "exec", "shell", "http", "browser"}
_WAITING_STATUSES = {
    "confirmation_required",
    "idempotency_key_required",
    "idempotency_key_required_after_confirmation",
}


class WorkflowSkillRetryPolicy(BaseModel):
    """Workflow step retry declaration."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=0, ge=0)


class WorkflowSkillStep(BaseModel):
    """One deterministic workflow step backed by a governed tool."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    retry: WorkflowSkillRetryPolicy = Field(default_factory=WorkflowSkillRetryPolicy)
    checkpoint: bool = False
    confirmation: bool = False
    idempotency: Literal["none", "optional", "required"] = "none"


class WorkflowSkillManifest(BaseModel):
    """Validated workflow skill manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["workflow_skill_v1"]
    name: str = Field(min_length=1)
    type: Literal["workflow"]
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    steps: list[WorkflowSkillStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_step_ids(self) -> "WorkflowSkillManifest":
        seen: set[str] = set()
        duplicates: list[str] = []
        for step in self.steps:
            if step.id in seen:
                duplicates.append(step.id)
            seen.add(step.id)
        if duplicates:
            raise ValueError(f"duplicate workflow step id: {duplicates[0]}")
        return self


class WorkflowSkillValidationIssue(BaseModel):
    """Prompt-safe workflow skill validation issue."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    step_id: str | None = None
    tool_name: str | None = None


class WorkflowSkillValidationResult(BaseModel):
    """Validation result for one workflow skill manifest."""

    accepted: bool
    manifest: WorkflowSkillManifest | None = None
    issues: list[WorkflowSkillValidationIssue] = Field(default_factory=list)


class WorkflowSkillCatalog:
    """Explicitly registered workflow skill manifests for one runtime boundary."""

    def __init__(self, *, registry: ToolRegistry) -> None:
        self.registry = registry
        self._manifests: dict[str, WorkflowSkillManifest] = {}

    def register(
        self,
        payload: Mapping[str, Any] | WorkflowSkillManifest,
    ) -> WorkflowSkillValidationResult:
        """Validate and register a workflow manifest by its manifest name."""

        validation = validate_workflow_skill_manifest(
            _manifest_payload(payload),
            registry=self.registry,
        )
        if validation.accepted and validation.manifest is not None:
            self._manifests[validation.manifest.name] = validation.manifest
        return validation

    def get(self, workflow_id: str) -> WorkflowSkillManifest | None:
        """Return a registered workflow manifest by id."""

        return self._manifests.get(workflow_id)

    def list_workflow_ids(self) -> list[str]:
        """Return registered workflow ids in registration order."""

        return list(self._manifests)


class WorkflowSkillAttemptRecord(BaseModel):
    """Prompt-safe record for one workflow step attempt."""

    workflow_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    tool_name: str = Field(min_length=1)
    status: WorkflowSkillAttemptStatus
    retry_count: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    output_ref: str | None = None
    error_summary: str | None = None
    validation_code: str | None = None


class WorkflowSkillRunResult(BaseModel):
    """Result of executing a workflow skill."""

    success: bool
    status: WorkflowSkillRunStatus
    workflow_id: str = Field(min_length=1)
    attempts: list[WorkflowSkillAttemptRecord] = Field(default_factory=list)
    step_results: dict[str, ToolResult] = Field(default_factory=dict)
    issues: list[WorkflowSkillValidationIssue] = Field(default_factory=list)


class WorkflowSkillRunRecord(BaseModel):
    """Checkpointable workflow run record."""

    workflow_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: WorkflowSkillRunStatus
    attempts: list[WorkflowSkillAttemptRecord] = Field(default_factory=list)
    step_results: dict[str, ToolResult] = Field(default_factory=dict)
    issues: list[WorkflowSkillValidationIssue] = Field(default_factory=list)
    completed_step_ids: list[str] = Field(default_factory=list)
    next_step_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WorkflowSkillRunSummary(BaseModel):
    """Operator-facing workflow run summary without raw step outputs."""

    workflow_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: WorkflowSkillRunStatus
    attempt_count: int = Field(ge=0)
    completed_step_ids: list[str] = Field(default_factory=list)
    next_step_id: str | None = None
    last_error_summary: str | None = None
    issue_codes: list[str] = Field(default_factory=list)
    updated_at: datetime


class InMemoryWorkflowSkillRunStore:
    """Process-local workflow skill run store for deterministic resume tests."""

    def __init__(self) -> None:
        self._records: dict[str, WorkflowSkillRunRecord] = {}

    def save(self, record: WorkflowSkillRunRecord) -> WorkflowSkillRunRecord:
        """Store or replace one workflow run record."""

        self._records[record.run_id] = record
        return record

    def get(self, run_id: str) -> WorkflowSkillRunRecord | None:
        """Return one workflow run record by run id."""

        return self._records.get(run_id)

    def list_by_workflow(self, workflow_id: str) -> list[WorkflowSkillRunRecord]:
        """Return records for one workflow id in insertion order."""

        return [
            record
            for record in self._records.values()
            if record.workflow_id == workflow_id
        ]

    def save_result(
        self,
        result: WorkflowSkillRunResult,
        *,
        state: AgentState,
        existing: WorkflowSkillRunRecord | None = None,
    ) -> WorkflowSkillRunRecord:
        """Persist a workflow run result as a checkpointable record."""

        return self.save(_record_from_result(result, state=state, existing=existing))


class JsonlWorkflowSkillRunStore:
    """JSONL-backed workflow skill run store for explicit local durability."""

    def __init__(self, path: Path | str = ".local/workflow_skill_runs.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, record: WorkflowSkillRunRecord) -> WorkflowSkillRunRecord:
        """Append one workflow run record snapshot."""

        with self.path.open("a", encoding="utf-8") as file:
            file.write(
                json.dumps(
                    {
                        "kind": "workflow_skill_run_record",
                        "payload": record.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return record

    def get(self, run_id: str) -> WorkflowSkillRunRecord | None:
        """Return the latest workflow run record by run id."""

        for record in reversed(self._read_records()):
            if record.run_id == run_id:
                return record
        return None

    def list_by_workflow(self, workflow_id: str) -> list[WorkflowSkillRunRecord]:
        """Return latest records for one workflow id in insertion order."""

        return [
            record
            for record in self._latest_records()
            if record.workflow_id == workflow_id
        ]

    def save_result(
        self,
        result: WorkflowSkillRunResult,
        *,
        state: AgentState,
        existing: WorkflowSkillRunRecord | None = None,
    ) -> WorkflowSkillRunRecord:
        """Persist a workflow run result as a checkpointable record."""

        return self.save(_record_from_result(result, state=state, existing=existing))

    def _latest_records(self) -> list[WorkflowSkillRunRecord]:
        latest: dict[str, WorkflowSkillRunRecord] = {}
        for record in self._read_records():
            latest[record.run_id] = record
        return list(latest.values())

    def _read_records(self) -> list[WorkflowSkillRunRecord]:
        if not self.path.exists():
            return []
        records: list[WorkflowSkillRunRecord] = []
        with self.path.open("r", encoding="utf-8") as file:
            for lineno, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    envelope = json.loads(stripped)
                    if not isinstance(envelope, dict):
                        continue
                    if envelope.get("kind") != "workflow_skill_run_record":
                        continue
                    payload = envelope.get("payload")
                    if isinstance(payload, dict):
                        records.append(WorkflowSkillRunRecord.model_validate(payload))
                except (json.JSONDecodeError, ValidationError):
                    logger.warning(
                        "Skipping invalid workflow skill run record in %s at line %s",
                        self.path,
                        lineno,
                    )
        return records


class WorkflowSkillRunQueryService:
    """Prompt-safe workflow run query service."""

    def __init__(
        self,
        *,
        store: InMemoryWorkflowSkillRunStore | JsonlWorkflowSkillRunStore,
    ) -> None:
        self.store = store

    def get_run_summary(self, run_id: str) -> WorkflowSkillRunSummary | None:
        """Return one prompt-safe workflow run summary."""

        record = self.store.get(run_id)
        if record is None:
            return None
        return _run_summary_from_record(record)

    def list_run_summaries(self, workflow_id: str) -> list[WorkflowSkillRunSummary]:
        """Return prompt-safe summaries for one workflow id."""

        return [
            _run_summary_from_record(record)
            for record in self.store.list_by_workflow(workflow_id)
        ]


def validate_workflow_skill_manifest(
    payload: Mapping[str, Any],
    *,
    registry: ToolRegistry,
) -> WorkflowSkillValidationResult:
    """Validate a workflow skill manifest against the governed tool registry."""

    issues = _pre_validation_issues(payload)
    try:
        manifest = WorkflowSkillManifest.model_validate(payload)
    except ValidationError as exc:
        issues.append(
            WorkflowSkillValidationIssue(
                code="manifest_invalid",
                message=_validation_error_message(exc),
            )
        )
        return WorkflowSkillValidationResult(
            accepted=False,
            manifest=None,
            issues=issues,
        )

    issues.extend(_policy_validation_issues(manifest, registry=registry))
    return WorkflowSkillValidationResult(
        accepted=not issues,
        manifest=manifest if not issues else None,
        issues=issues,
    )


class WorkflowSkillRunner:
    """Execute workflow skill steps through the governed tool boundary."""

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
            raise ValueError("WorkflowSkillRunner and ToolExecutor must use the same registry")

    def run(
        self,
        payload: Mapping[str, Any] | WorkflowSkillManifest,
        state: AgentState,
        *,
        resume_record: WorkflowSkillRunRecord | None = None,
    ) -> WorkflowSkillRunResult:
        manifest = _manifest_from_payload(payload)
        if manifest is None:
            validation = validate_workflow_skill_manifest(
                payload if isinstance(payload, Mapping) else payload.model_dump(mode="python"),
                registry=self.registry,
            )
            return WorkflowSkillRunResult(
                success=False,
                status="validation_failed",
                workflow_id=_payload_name(payload),
                issues=validation.issues,
            )
        validation = validate_workflow_skill_manifest(
            manifest.model_dump(mode="python"),
            registry=self.registry,
        )
        if not validation.accepted:
            return WorkflowSkillRunResult(
                success=False,
                status="validation_failed",
                workflow_id=manifest.name,
                issues=validation.issues,
            )

        attempts: list[WorkflowSkillAttemptRecord] = (
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
        return WorkflowSkillRunResult(
            success=True,
            status="succeeded",
            workflow_id=manifest.name,
            attempts=attempts,
            step_results=step_results,
        )

    def _run_step(
        self,
        *,
        manifest: WorkflowSkillManifest,
        step: WorkflowSkillStep,
        state: AgentState,
        attempts: list[WorkflowSkillAttemptRecord],
        step_results: dict[str, ToolResult],
    ) -> WorkflowSkillRunResult | None:
        policy_view = ToolPolicyInterpreter().view_for_spec(self.registry.get_spec(step.tool))
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
                reason=f"workflow_skill:{manifest.name}:{step.id}",
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
                return WorkflowSkillRunResult(
                    success=False,
                    status="rejected",
                    workflow_id=manifest.name,
                    attempts=attempts,
                    step_results=step_results,
                )

            result = self.tool_executor.run_tool(
                state,
                step.id,
                step.tool,
                tool_input,
                trace_id=state.trace_id,
                node_name="workflow_skill_runner",
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
            if status == "waiting_confirmation":
                step_results[step.id] = result
                return WorkflowSkillRunResult(
                    success=False,
                    status="waiting_confirmation",
                    workflow_id=manifest.name,
                    attempts=attempts,
                    step_results=step_results,
                )
            if attempt_number > step.retry.max_retries or not _step_retry_allowed(
                result,
                policy_view=policy_view,
            ):
                step_results[step.id] = result
                return WorkflowSkillRunResult(
                    success=False,
                    status="failed",
                    workflow_id=manifest.name,
                    attempts=attempts,
                    step_results=step_results,
                )
        return None


class WorkflowSkillLauncher:
    """Launch only explicitly registered workflow manifests."""

    def __init__(
        self,
        *,
        catalog: WorkflowSkillCatalog,
        runner: WorkflowSkillRunner | None = None,
        run_store: InMemoryWorkflowSkillRunStore | JsonlWorkflowSkillRunStore | None = None,
    ) -> None:
        self.catalog = catalog
        self.runner = runner or WorkflowSkillRunner(registry=catalog.registry)
        self.run_store = run_store or InMemoryWorkflowSkillRunStore()
        if self.runner.registry is not catalog.registry:
            raise ValueError(
                "WorkflowSkillLauncher and WorkflowSkillCatalog must use the same registry"
            )

    def launch(self, workflow_id: str, state: AgentState) -> WorkflowSkillRunResult:
        """Launch a registered workflow manifest by id."""

        manifest = self.catalog.get(workflow_id)
        if manifest is None:
            safe_workflow_id = workflow_id or "unknown"
            return WorkflowSkillRunResult(
                success=False,
                status="validation_failed",
                workflow_id=safe_workflow_id,
                issues=[
                    WorkflowSkillValidationIssue(
                        code="workflow_not_registered",
                        message="Workflow skill manifest is not explicitly registered.",
                    )
                ],
            )
        result = self.runner.run(manifest, state)
        self.run_store.save_result(result, state=state)
        return result

    def resume(self, run_id: str, state: AgentState) -> WorkflowSkillRunResult:
        """Resume a workflow run from its latest checkpointable record."""

        record = self.run_store.get(run_id)
        if record is None:
            return WorkflowSkillRunResult(
                success=False,
                status="validation_failed",
                workflow_id="unknown",
                issues=[
                    WorkflowSkillValidationIssue(
                        code="workflow_run_not_found",
                        message="Workflow skill run record was not found.",
                    )
                ],
            )
        manifest = self.catalog.get(record.workflow_id)
        if manifest is None:
            return WorkflowSkillRunResult(
                success=False,
                status="validation_failed",
                workflow_id=record.workflow_id,
                attempts=list(record.attempts),
                step_results=dict(record.step_results),
                issues=[
                    WorkflowSkillValidationIssue(
                        code="workflow_not_registered",
                        message="Workflow skill manifest is not explicitly registered.",
                    )
                ],
            )
        state.run_id = record.run_id
        result = self.runner.run(manifest, state, resume_record=record)
        self.run_store.save_result(result, state=state, existing=record)
        return result

    def get_run(self, run_id: str) -> WorkflowSkillRunRecord | None:
        """Return one workflow run record by run id."""

        return self.run_store.get(run_id)


def _pre_validation_issues(payload: Mapping[str, Any]) -> list[WorkflowSkillValidationIssue]:
    issues: list[WorkflowSkillValidationIssue] = []
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return issues
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        if _UNSUPPORTED_STEP_KEYS.intersection(raw_step):
            issues.append(
                WorkflowSkillValidationIssue(
                    code="unsupported_step_action",
                    message="Workflow steps may only declare governed tool calls.",
                    step_id=str(raw_step.get("id") or index),
                )
            )
    return issues


def _policy_validation_issues(
    manifest: WorkflowSkillManifest,
    *,
    registry: ToolRegistry,
) -> list[WorkflowSkillValidationIssue]:
    issues: list[WorkflowSkillValidationIssue] = []
    permissions = set(manifest.permissions)
    for permission in manifest.permissions:
        if not permission.startswith("tool:"):
            issues.append(
                WorkflowSkillValidationIssue(
                    code="invalid_permission",
                    message="Workflow skill permissions must use tool:<name> vocabulary.",
                )
            )
    interpreter = ToolPolicyInterpreter()
    for step in manifest.steps:
        if step.tool not in registry.list():
            issues.append(
                WorkflowSkillValidationIssue(
                    code="unknown_tool",
                    message=f"Unknown workflow step tool: {step.tool}.",
                    step_id=step.id,
                    tool_name=step.tool,
                )
            )
            continue
        if permissions and f"tool:{step.tool}" not in permissions:
            issues.append(
                WorkflowSkillValidationIssue(
                    code="missing_tool_permission",
                    message="Every workflow step tool must have a matching tool:<name> permission.",
                    step_id=step.id,
                    tool_name=step.tool,
                )
            )
        policy_view = interpreter.view_for_spec(registry.get_spec(step.tool))
        if (
            step.retry.max_retries > 0
            and not _policy_replay_safe(policy_view, step=step)
        ):
            issues.append(
                WorkflowSkillValidationIssue(
                    code="step_retry_requires_idempotency",
                    message="Retrying a mutating workflow step requires idempotency=required.",
                    step_id=step.id,
                    tool_name=step.tool,
                )
            )
    return issues


def _policy_replay_safe(policy_view: ToolPolicyView, *, step: WorkflowSkillStep) -> bool:
    if policy_view.side_effect_level in _READ_ONLY_LEVELS:
        return True
    return step.idempotency == "required"


def _resume_can_skip_step(
    step: WorkflowSkillStep,
    *,
    completed_step_ids: set[str],
    step_results: dict[str, ToolResult],
) -> bool:
    return step.checkpoint and step.id in completed_step_ids and step.id in step_results


def _record_from_result(
    result: WorkflowSkillRunResult,
    *,
    state: AgentState,
    existing: WorkflowSkillRunRecord | None = None,
) -> WorkflowSkillRunRecord:
    created_at = existing.created_at if existing is not None else datetime.now(timezone.utc)
    return WorkflowSkillRunRecord(
        workflow_id=result.workflow_id,
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


def _completed_step_ids(attempts: list[WorkflowSkillAttemptRecord]) -> list[str]:
    completed: list[str] = []
    for attempt in attempts:
        if attempt.status == "succeeded" and attempt.step_id not in completed:
            completed.append(attempt.step_id)
    return completed


def _run_summary_from_record(record: WorkflowSkillRunRecord) -> WorkflowSkillRunSummary:
    return WorkflowSkillRunSummary(
        workflow_id=record.workflow_id,
        run_id=record.run_id,
        status=record.status,
        attempt_count=len(record.attempts),
        completed_step_ids=list(record.completed_step_ids),
        next_step_id=record.next_step_id,
        last_error_summary=_last_error_summary(record.attempts),
        issue_codes=[issue.code for issue in record.issues],
        updated_at=record.updated_at,
    )


def _last_error_summary(attempts: list[WorkflowSkillAttemptRecord]) -> str | None:
    for attempt in reversed(attempts):
        if attempt.error_summary:
            return attempt.error_summary
    return None


def _next_step_id(result: WorkflowSkillRunResult) -> str | None:
    if result.status == "waiting_confirmation":
        for attempt in reversed(result.attempts):
            if attempt.status == "waiting_confirmation":
                return attempt.step_id
    if result.status in {"failed", "rejected"}:
        for attempt in reversed(result.attempts):
            if attempt.status in {"failed", "rejected"}:
                return attempt.step_id
    return None


def _manifest_from_payload(payload: Mapping[str, Any] | WorkflowSkillManifest) -> WorkflowSkillManifest | None:
    if isinstance(payload, WorkflowSkillManifest):
        return payload
    try:
        return WorkflowSkillManifest.model_validate(payload)
    except ValidationError:
        return None


def _manifest_payload(payload: Mapping[str, Any] | WorkflowSkillManifest) -> Mapping[str, Any]:
    if isinstance(payload, WorkflowSkillManifest):
        return payload.model_dump(mode="python")
    return payload


def _payload_name(payload: Mapping[str, Any] | WorkflowSkillManifest) -> str:
    if isinstance(payload, WorkflowSkillManifest):
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
    manifest: WorkflowSkillManifest,
    step: WorkflowSkillStep,
    state: AgentState,
) -> dict[str, Any]:
    if step.idempotency != "required" or tool_input.get("idempotency_key"):
        return tool_input
    return {
        **tool_input,
        "idempotency_key": f"workflow:{manifest.name}:{state.run_id}:{step.id}",
    }


def _attempt_status(result: ToolResult) -> WorkflowSkillAttemptStatus:
    if result.success and isinstance(result.data, dict) and result.data.get("status") in _WAITING_STATUSES:
        return "waiting_confirmation"
    if result.success:
        return "succeeded"
    return "failed"


def _attempt_record(
    *,
    manifest: WorkflowSkillManifest,
    state: AgentState,
    step: WorkflowSkillStep,
    attempt_number: int,
    status: WorkflowSkillAttemptStatus,
    tool_input: dict[str, Any],
    result: ToolResult | None = None,
    validation_code: str | None = None,
    error_summary: str | None = None,
) -> WorkflowSkillAttemptRecord:
    retry_count = 0
    if result is not None and isinstance(result.trace_summary, dict):
        raw_retry_count = result.trace_summary.get("retry_count")
        if isinstance(raw_retry_count, int) and raw_retry_count >= 0:
            retry_count = raw_retry_count
    return WorkflowSkillAttemptRecord(
        workflow_id=manifest.name,
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


def _step_retry_allowed(result: ToolResult, *, policy_view: ToolPolicyView) -> bool:
    if result.success:
        return False
    if policy_view.side_effect_level not in _READ_ONLY_LEVELS and result.data and result.data.get("status") == "unknown_after_timeout":
        return False
    return True


def _validation_error_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {"msg": "invalid manifest"}
    message = first.get("msg")
    return str(message or "invalid manifest")


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
