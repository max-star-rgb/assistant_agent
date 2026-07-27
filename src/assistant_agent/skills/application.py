"""Product runtime boundary for explicit skill entrypoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field

from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.multi_agent.agent_control_plane import InMemoryAgentControlPlaneStore
from assistant_agent.skills.runtime import (
    InMemorySkillRunStore,
    JsonlSkillRunStore,
    SkillCatalog,
    SkillLauncher,
    SkillRunQueryService,
    SkillRunStatus,
    SkillRunSummary,
    SkillValidationIssue,
)
from assistant_agent.tools.loader import LocalToolLoadIssue, load_local_tools, register_local_tools
from assistant_agent.tools.registry import ToolRegistry


SKILLS_ENABLED_ENV = "MULTIMODAL_AGENT_SKILLS_ENABLED"
SKILL_MANIFEST_DIR_ENV = "MULTIMODAL_AGENT_SKILL_MANIFEST_DIR"
SKILL_TOOL_MODULES_ENV = "MULTIMODAL_AGENT_SKILL_TOOL_MODULES"
SKILL_RUN_STORE_ENV = "MULTIMODAL_AGENT_SKILL_RUN_STORE"
DEFAULT_SKILL_MANIFEST_DIR = "skills/manifests"
DEFAULT_SKILL_RUN_STORE = ".data/skill_runs.jsonl"
REPO_ROOT = Path(__file__).resolve().parents[3]


class SkillRuntimeIssue(BaseModel):
    """Prompt-safe skill runtime configuration/load issue."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    path: str | None = None
    module: str | None = None
    skill_id: str | None = None
    tool_name: str | None = None


class SkillInfo(BaseModel):
    """Prompt-safe skill manifest catalog entry."""

    skill_id: str = Field(min_length=1)
    description: str = ""
    step_count: int = Field(ge=0)
    tool_names: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class SkillOperationResult(BaseModel):
    """Prompt-safe skill launch/resume result."""

    success: bool
    status: SkillRunStatus
    skill_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    attempt_count: int = Field(ge=0)
    issues: list[SkillValidationIssue] = Field(default_factory=list)
    issue_codes: list[str] = Field(default_factory=list)
    summary: SkillRunSummary | None = None


class SkillRuntimeApp:
    """Product entry boundary over skill catalog/launcher/query."""

    def __init__(
        self,
        *,
        enabled: bool,
        manifest_dir: Path,
        tool_modules: list[str],
        run_store_path: Path,
        registry: ToolRegistry | None = None,
        catalog: SkillCatalog | None = None,
        run_store: InMemorySkillRunStore | JsonlSkillRunStore | None = None,
        audit_store: InMemoryAgentControlPlaneStore | None = None,
        issues: list[SkillRuntimeIssue] | None = None,
    ) -> None:
        self.enabled = enabled
        self.manifest_dir = manifest_dir
        self.tool_modules = list(tool_modules)
        self.run_store_path = run_store_path
        self.registry = registry or ToolRegistry()
        self.catalog = catalog or SkillCatalog(registry=self.registry)
        if run_store is not None:
            self.run_store = run_store
        elif enabled:
            self.run_store = JsonlSkillRunStore(run_store_path)
        else:
            self.run_store = InMemorySkillRunStore()
        self.audit_store = audit_store or InMemoryAgentControlPlaneStore()
        self.issues = list(issues or [])
        self.launcher = SkillLauncher(
            catalog=self.catalog,
            run_store=self.run_store,
            audit_sink=self.audit_store,
        )
        self.query_service = SkillRunQueryService(
            store=self.run_store,
            audit_sink=self.audit_store,
        )

    @classmethod
    def disabled(cls, *, base_dir: Path | None = None) -> "SkillRuntimeApp":
        resolved_base = base_dir or REPO_ROOT
        return cls(
            enabled=False,
            manifest_dir=_resolve_path(DEFAULT_SKILL_MANIFEST_DIR, base_dir=resolved_base),
            tool_modules=[],
            run_store_path=_resolve_path(DEFAULT_SKILL_RUN_STORE, base_dir=resolved_base),
        )

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        base_dir: Path | None = None,
    ) -> "SkillRuntimeApp":
        source = os.environ if env is None else env
        resolved_base = base_dir or REPO_ROOT
        enabled = _truthy(source.get(SKILLS_ENABLED_ENV))
        manifest_dir = _resolve_path(
            source.get(SKILL_MANIFEST_DIR_ENV) or DEFAULT_SKILL_MANIFEST_DIR,
            base_dir=resolved_base,
        )
        tool_modules = _module_names(source.get(SKILL_TOOL_MODULES_ENV))
        run_store_path = _resolve_path(
            source.get(SKILL_RUN_STORE_ENV) or DEFAULT_SKILL_RUN_STORE,
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
        issues: list[SkillRuntimeIssue] = []
        tool_load_result = load_local_tools(tool_modules)
        issues.extend(_tool_load_issues(tool_load_result.issues))
        for local_tool in tool_load_result.tools:
            issues.extend(_local_tool_policy_issues(local_tool))
        if not issues:
            register_local_tools(registry, tool_load_result.tools)

        catalog = SkillCatalog(registry=registry)
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

    def list_skills(self) -> list[SkillInfo]:
        """Return prompt-safe registered skill descriptors."""

        skills: list[SkillInfo] = []
        for skill_id in self.catalog.list_skill_ids():
            manifest = self.catalog.get(skill_id)
            if manifest is None:
                continue
            skills.append(
                SkillInfo(
                    skill_id=manifest.name,
                    description=manifest.description,
                    step_count=len(manifest.steps),
                    tool_names=[step.tool for step in manifest.steps],
                    permissions=list(manifest.permissions),
                )
            )
        return skills

    def has_skill(self, skill_id: str) -> bool:
        return self.catalog.get(skill_id) is not None

    def has_run(self, run_id: str) -> bool:
        return self.launcher.get_run(run_id) is not None

    def launch(
        self,
        skill_id: str,
        *,
        text: str = "",
        user_id: str = "skill-api",
        session_id: str = "skill-api",
        run_id: str | None = None,
    ) -> SkillOperationResult:
        state = AgentState.from_request(
            UserRequest(
                user_id=user_id,
                session_id=session_id,
                text=text,
                metadata={"source": "skill_api"},
            ),
            run_id=run_id,
        )
        result = self.launcher.launch(skill_id, state)
        return _operation_result(result, run_id=state.run_id, summary=self.summary(state.run_id))

    def resume(
        self,
        run_id: str,
        *,
        text: str = "",
        user_id: str = "skill-api",
        session_id: str = "skill-api",
    ) -> SkillOperationResult:
        result = self.launcher.resume(
            run_id,
            AgentState.from_request(
                UserRequest(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                    metadata={"source": "skill_api"},
                )
            ),
        )
        return _operation_result(result, run_id=run_id, summary=self.summary(run_id))

    def summary(self, run_id: str) -> SkillRunSummary | None:
        return self.query_service.get_run_summary(run_id)

    def list_run_summaries(self, skill_id: str) -> list[SkillRunSummary]:
        return self.query_service.list_run_summaries(skill_id)


def create_skill_runtime_app_from_env() -> SkillRuntimeApp:
    """Create skill runtime app from process environment."""

    return SkillRuntimeApp.from_env()


def _operation_result(
    result: Any,
    *,
    run_id: str,
    summary: SkillRunSummary | None,
) -> SkillOperationResult:
    return SkillOperationResult(
        success=bool(result.success),
        status=result.status,
        skill_id=result.skill_id,
        run_id=run_id,
        attempt_count=len(result.attempts),
        issues=list(result.issues),
        issue_codes=[issue.code for issue in result.issues],
        summary=summary,
    )


def _register_manifests(
    catalog: SkillCatalog,
    manifest_dir: Path,
) -> list[SkillRuntimeIssue]:
    if not manifest_dir.exists():
        return [
            SkillRuntimeIssue(
                code="manifest_dir_missing",
                message="Skill manifest directory does not exist.",
                path=str(manifest_dir),
            )
        ]
    issues: list[SkillRuntimeIssue] = []
    for path in sorted(manifest_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            issues.append(
                SkillRuntimeIssue(
                    code="invalid_manifest_json",
                    message=str(exc),
                    path=str(path),
                )
            )
            continue
        if not isinstance(payload, dict):
            issues.append(
                SkillRuntimeIssue(
                    code="invalid_manifest_json",
                    message="Skill manifest must be a JSON object.",
                    path=str(path),
                )
            )
            continue
        validation = catalog.register(payload)
        issues.extend(
            _validation_issues(
                validation.issues,
                path=path,
                skill_id=str(payload.get("name") or ""),
            )
        )
    return issues


def _tool_load_issues(issues: list[LocalToolLoadIssue]) -> list[SkillRuntimeIssue]:
    return [
        SkillRuntimeIssue(
            code=issue.code,
            message=issue.message,
            module=issue.module or None,
            tool_name=issue.tool_name,
        )
        for issue in issues
    ]


def _local_tool_policy_issues(tool: Any) -> list[SkillRuntimeIssue]:
    try:
        ToolRegistry._tool_spec(tool)
    except Exception as exc:
        return [
            SkillRuntimeIssue(
                code="invalid_tool_spec",
                message=str(exc),
                tool_name=getattr(tool, "name", ""),
            )
        ]
    return []


def _validation_issues(
    issues: list[SkillValidationIssue],
    *,
    path: Path,
    skill_id: str,
) -> list[SkillRuntimeIssue]:
    return [
        SkillRuntimeIssue(
            code=issue.code,
            message=issue.message,
            path=str(path),
            skill_id=skill_id or None,
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
