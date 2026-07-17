"""Product runtime boundary for explicit workflow skill entrypoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field

from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.agent_control_plane import InMemoryAgentControlPlaneStore
from assistant_agent.services.tool_workflow_skill import (
    InMemoryWorkflowSkillRunStore,
    JsonlWorkflowSkillRunStore,
    WorkflowSkillCatalog,
    WorkflowSkillLauncher,
    WorkflowSkillRunQueryService,
    WorkflowSkillRunStatus,
    WorkflowSkillRunSummary,
    WorkflowSkillValidationIssue,
)
from assistant_agent.tools.loader import LocalToolLoadIssue, load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry, tool_policy_metadata


WORKFLOW_SKILLS_ENABLED_ENV = "MULTIMODAL_AGENT_WORKFLOW_SKILLS_ENABLED"
WORKFLOW_SKILL_MANIFEST_DIR_ENV = "MULTIMODAL_AGENT_WORKFLOW_SKILL_MANIFEST_DIR"
WORKFLOW_SKILL_TOOL_MODULES_ENV = "MULTIMODAL_AGENT_WORKFLOW_SKILL_TOOL_MODULES"
WORKFLOW_SKILL_RUN_STORE_ENV = "MULTIMODAL_AGENT_WORKFLOW_SKILL_RUN_STORE"
DEFAULT_WORKFLOW_SKILL_MANIFEST_DIR = "skills/workflows"
DEFAULT_WORKFLOW_SKILL_RUN_STORE = ".data/workflow_skill_runs.jsonl"
REPO_ROOT = Path(__file__).resolve().parents[3]


class WorkflowSkillRuntimeIssue(BaseModel):
    """Prompt-safe workflow runtime configuration/load issue."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None
    module: str | None = None
    workflow_id: str | None = None
    tool_name: str | None = None


class WorkflowSkillInfo(BaseModel):
    """Prompt-safe workflow manifest catalog entry."""

    workflow_id: str = Field(min_length=1)
    description: str = ""
    step_count: int = Field(ge=0)
    tool_names: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class WorkflowSkillOperationResult(BaseModel):
    """Prompt-safe workflow launch/resume result."""

    success: bool
    status: WorkflowSkillRunStatus
    workflow_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_count: int = Field(ge=0)
    issues: list[WorkflowSkillValidationIssue] = Field(default_factory=list)
    issue_codes: list[str] = Field(default_factory=list)
    summary: WorkflowSkillRunSummary | None = None


class WorkflowSkillRuntimeApp:
    """Product entry boundary over workflow skill catalog/launcher/query."""

    def __init__(
        self,
        *,
        enabled: bool,
        manifest_dir: Path,
        tool_modules: list[str],
        run_store_path: Path,
        registry: ToolRegistry | None = None,
        catalog: WorkflowSkillCatalog | None = None,
        run_store: InMemoryWorkflowSkillRunStore | JsonlWorkflowSkillRunStore | None = None,
        audit_store: InMemoryAgentControlPlaneStore | None = None,
        issues: list[WorkflowSkillRuntimeIssue] | None = None,
    ) -> None:
        self.enabled = enabled
        self.manifest_dir = manifest_dir
        self.tool_modules = list(tool_modules)
        self.run_store_path = run_store_path
        self.registry = registry or ToolRegistry()
        self.catalog = catalog or WorkflowSkillCatalog(registry=self.registry)
        if run_store is not None:
            self.run_store = run_store
        elif enabled:
            self.run_store = JsonlWorkflowSkillRunStore(run_store_path)
        else:
            self.run_store = InMemoryWorkflowSkillRunStore()
        self.audit_store = audit_store or InMemoryAgentControlPlaneStore()
        self.issues = list(issues or [])
        self.launcher = WorkflowSkillLauncher(
            catalog=self.catalog,
            run_store=self.run_store,
            audit_sink=self.audit_store,
        )
        self.query_service = WorkflowSkillRunQueryService(
            store=self.run_store,
            audit_sink=self.audit_store,
        )

    @classmethod
    def disabled(cls, *, base_dir: Path | None = None) -> "WorkflowSkillRuntimeApp":
        resolved_base = base_dir or REPO_ROOT
        return cls(
            enabled=False,
            manifest_dir=_resolve_path(DEFAULT_WORKFLOW_SKILL_MANIFEST_DIR, base_dir=resolved_base),
            tool_modules=[],
            run_store_path=_resolve_path(DEFAULT_WORKFLOW_SKILL_RUN_STORE, base_dir=resolved_base),
        )

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        base_dir: Path | None = None,
    ) -> "WorkflowSkillRuntimeApp":
        source = os.environ if env is None else env
        resolved_base = base_dir or REPO_ROOT
        enabled = _truthy(source.get(WORKFLOW_SKILLS_ENABLED_ENV))
        manifest_dir = _resolve_path(
            source.get(WORKFLOW_SKILL_MANIFEST_DIR_ENV) or DEFAULT_WORKFLOW_SKILL_MANIFEST_DIR,
            base_dir=resolved_base,
        )
        tool_modules = _module_names(source.get(WORKFLOW_SKILL_TOOL_MODULES_ENV))
        run_store_path = _resolve_path(
            source.get(WORKFLOW_SKILL_RUN_STORE_ENV) or DEFAULT_WORKFLOW_SKILL_RUN_STORE,
            base_dir=resolved_base,
        )
        if not enabled:
            return cls(
                enabled=False,
                manifest_dir=manifest_dir,
                tool_modules=tool_modules,
                run_store_path=run_store_path,
            )

        registry = ToolRegistry()
        issues: list[WorkflowSkillRuntimeIssue] = []
        tool_load_result = load_local_tools(tool_modules)
        issues.extend(_tool_load_issues(tool_load_result.issues))
        for local_tool in tool_load_result.tools:
            issues.extend(_local_tool_policy_issues(local_tool))
        if not issues:
            register_local_tools(registry, tool_load_result.tools)

        catalog = WorkflowSkillCatalog(registry=registry)
        issues.extend(_register_manifests(catalog, manifest_dir))
        return cls(
            enabled=True,
            manifest_dir=manifest_dir,
            tool_modules=tool_modules,
            run_store_path=run_store_path,
            registry=registry,
            catalog=catalog,
            issues=issues,
        )

    def list_workflows(self) -> list[WorkflowSkillInfo]:
        """Return prompt-safe registered workflow descriptors."""

        workflows: list[WorkflowSkillInfo] = []
        for workflow_id in self.catalog.list_workflow_ids():
            manifest = self.catalog.get(workflow_id)
            if manifest is None:
                continue
            workflows.append(
                WorkflowSkillInfo(
                    workflow_id=manifest.name,
                    description=manifest.description,
                    step_count=len(manifest.steps),
                    tool_names=[step.tool for step in manifest.steps],
                    permissions=list(manifest.permissions),
                )
            )
        return workflows

    def has_workflow(self, workflow_id: str) -> bool:
        return self.catalog.get(workflow_id) is not None

    def has_run(self, run_id: str) -> bool:
        return self.launcher.get_run(run_id) is not None

    def launch(
        self,
        workflow_id: str,
        *,
        text: str = "",
        user_id: str = "workflow-api",
        session_id: str = "workflow-api",
        run_id: str | None = None,
    ) -> WorkflowSkillOperationResult:
        state = AgentState.from_request(
            UserRequest(
                user_id=user_id,
                session_id=session_id,
                text=text,
                metadata={"source": "workflow_skill_api"},
            ),
            run_id=run_id,
        )
        result = self.launcher.launch(workflow_id, state)
        return _operation_result(result, run_id=state.run_id, summary=self.summary(state.run_id))

    def resume(
        self,
        run_id: str,
        *,
        text: str = "",
        user_id: str = "workflow-api",
        session_id: str = "workflow-api",
    ) -> WorkflowSkillOperationResult:
        result = self.launcher.resume(
            run_id,
            AgentState.from_request(
                UserRequest(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                    metadata={"source": "workflow_skill_api"},
                )
            ),
        )
        return _operation_result(result, run_id=run_id, summary=self.summary(run_id))

    def summary(self, run_id: str) -> WorkflowSkillRunSummary | None:
        return self.query_service.get_run_summary(run_id)

    def list_run_summaries(self, workflow_id: str) -> list[WorkflowSkillRunSummary]:
        return self.query_service.list_run_summaries(workflow_id)


def create_workflow_skill_runtime_app_from_env() -> WorkflowSkillRuntimeApp:
    """Create workflow skill runtime app from process environment."""

    return WorkflowSkillRuntimeApp.from_env()


def _operation_result(
    result: Any,
    *,
    run_id: str,
    summary: WorkflowSkillRunSummary | None,
) -> WorkflowSkillOperationResult:
    return WorkflowSkillOperationResult(
        success=bool(result.success),
        status=result.status,
        workflow_id=result.workflow_id,
        run_id=run_id,
        attempt_count=len(result.attempts),
        issues=list(result.issues),
        issue_codes=[issue.code for issue in result.issues],
        summary=summary,
    )


def _register_manifests(
    catalog: WorkflowSkillCatalog,
    manifest_dir: Path,
) -> list[WorkflowSkillRuntimeIssue]:
    if not manifest_dir.exists():
        return [
            WorkflowSkillRuntimeIssue(
                code="manifest_dir_missing",
                message="Workflow skill manifest directory does not exist.",
                path=str(manifest_dir),
            )
        ]
    issues: list[WorkflowSkillRuntimeIssue] = []
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(
                WorkflowSkillRuntimeIssue(
                    code="invalid_manifest_json",
                    message=str(exc),
                    path=str(path),
                )
            )
            continue
        if not isinstance(payload, dict):
            issues.append(
                WorkflowSkillRuntimeIssue(
                    code="invalid_manifest_json",
                    message="Workflow manifest must be a JSON object.",
                    path=str(path),
                )
            )
            continue
        validation = catalog.register(payload)
        issues.extend(
            _validation_issues(
                validation.issues,
                path=path,
                workflow_id=str(payload.get("name") or ""),
            )
        )
    return issues


def _tool_load_issues(issues: list[LocalToolLoadIssue]) -> list[WorkflowSkillRuntimeIssue]:
    return [
        WorkflowSkillRuntimeIssue(
            code=issue.code,
            message=issue.message,
            module=issue.module or None,
            tool_name=issue.tool_name,
        )
        for issue in issues
    ]


def _local_tool_policy_issues(tool: Any) -> list[WorkflowSkillRuntimeIssue]:
    tool_name = getattr(tool, "name", "")
    try:
        policy = tool_policy_metadata(tool)
    except Exception as exc:
        return [
            WorkflowSkillRuntimeIssue(
                code="invalid_policy",
                message=str(exc),
                tool_name=tool_name,
            )
        ]
    if policy is None:
        return [
            WorkflowSkillRuntimeIssue(
                code="missing_policy",
                message="Workflow API local tools must declare ToolPolicyMetadata.",
                tool_name=tool_name,
            )
        ]
    return []


def _validation_issues(
    issues: list[WorkflowSkillValidationIssue],
    *,
    path: Path,
    workflow_id: str,
) -> list[WorkflowSkillRuntimeIssue]:
    return [
        WorkflowSkillRuntimeIssue(
            code=issue.code,
            message=issue.message,
            path=str(path),
            workflow_id=workflow_id or None,
            tool_name=issue.tool_name,
        )
        for issue in issues
    ]


def _module_names(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path(value: str, *, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path
