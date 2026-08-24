"""Deterministic contracts and aggregation for coding review workers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, NotRequired, Required

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict, Field, field_validator

from assistant_agent.coding.models import (
    CODING_REVIEW_TASK_SPECS,
    CodingAnalysisSnapshot,
    CodingReviewEvidence,
    CodingReviewFinding,
    CodingReviewInput,
    CodingReviewReport,
    CodingReviewTask,
    CodingReviewerResult,
)
from assistant_agent.coding.tools import build_coding_analysis_tools
from assistant_agent.coding.workspace import CodingWorkspaceService
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import coding_analysis_model_view

REVIEW_TASK_IDS = tuple(CODING_REVIEW_TASK_SPECS)
MAX_REVIEW_RESULT_JSON_CHARS = 16_000
MAX_REVIEW_REPORT_JSON_CHARS = 48_000
_SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "info": 4}
REVIEW_READ_TOOL_NAMES = (
    "coding_repo_diff",
    "coding_repo_list",
    "coding_repo_read",
    "coding_repo_search",
    "coding_repo_status",
)
_CODING_REVIEW_PROMPT = (
    "You are a final code-review worker. Review only the immutable, snapshot-bound "
    "workspace exposed by the provided coding_repo_* read tools. Return bounded "
    "structured findings for the assigned review dimension. Do not propose or apply "
    "patches, execute commands, validate, integrate, access the network, request "
    "credentials, or modify files."
)


class _ReviewEvidenceResponse(BaseModel):
    """Untrusted evidence before the worker binds it to its fixed review task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=240)
    line: int = Field(ge=1, le=10_000_000)
    excerpt: str = Field(min_length=1, max_length=800)


class _ReviewFindingResponse(BaseModel):
    """Untrusted finding shape returned by a read-only review worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    severity: Literal["critical", "high", "moderate", "low", "info"]
    category: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    summary: str = Field(min_length=1, max_length=1_200)
    evidence: tuple[_ReviewEvidenceResponse, ...] = Field(min_length=1, max_length=2)

    @field_validator("evidence", mode="before")
    @classmethod
    def _tuple_evidence(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingReviewResponse(BaseModel):
    """Public, untrusted structured response before trusted snapshot binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["succeeded", "failed", "stale"]
    findings: tuple[_ReviewFindingResponse, ...] = Field(max_length=8)
    error_code: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^coding_review_[a-z0-9_]+$",
    )

    @field_validator("findings", mode="before")
    @classmethod
    def _tuple_findings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class CodingReviewWorkerState(AgentState):
    """Narrow state delivered to one final snapshot review worker."""

    coding_repo_id: Required[str]
    workspace_ref: Required[str]
    base_commit: Required[str]
    analysis_snapshot: Required[CodingAnalysisSnapshot]
    review_input: Required[CodingReviewInput]
    review_task: Required[CodingReviewTask]
    provider_search_profile: Required[Literal["none"]]


class CodingReviewGraphState(AgentState):
    """Ephemeral final-review state, separate from the mutation graph state."""

    coding_repo_id: Required[str]
    workspace_ref: Required[str]
    base_commit: Required[str]
    review_snapshot: Required[CodingAnalysisSnapshot]
    review_input: Required[CodingReviewInput]
    review_tasks: NotRequired[tuple[CodingReviewTask, ...]]
    review_results: NotRequired[
        Annotated[list[CodingReviewerResult], merge_review_results]
    ]
    review_report: NotRequired[CodingReviewReport]
    review_status: NotRequired[Literal["clean", "findings", "unavailable"]]


def build_coding_review_tools(service: CodingWorkspaceService) -> list[BaseTool]:
    """Build the final-review-specific, snapshot-bound read-only tool profile."""

    tools = build_coding_analysis_tools(service)
    if tuple(tool.name for tool in tools) != REVIEW_READ_TOOL_NAMES:
        raise ValueError("coding_review_tool_profile_invalid")
    return tools


def build_review_tasks() -> tuple[CodingReviewTask, ...]:
    """Return the complete fixed review inventory in canonical order."""

    return tuple(
        CodingReviewTask(task_id=task_id, dimension=task_id, objective=objective)
        for task_id, objective in CODING_REVIEW_TASK_SPECS.items()
    )


def merge_review_results(
    current: Sequence[CodingReviewerResult] | None,
    update: Sequence[CodingReviewerResult] | None,
) -> list[CodingReviewerResult]:
    """Merge concurrent worker updates in fixed task order."""

    if update is not None and not update:
        return []
    by_task_id = {result.task_id: result for result in current or ()}
    by_task_id.update({result.task_id: result for result in update or ()})
    return [by_task_id[task_id] for task_id in REVIEW_TASK_IDS if task_id in by_task_id]


def prepare_review_tasks(state: Mapping[str, object]) -> dict[str, object]:
    """Freeze the final review input and its fixed three-worker inventory."""

    snapshot = CodingAnalysisSnapshot.model_validate(
        state.get("review_snapshot", state.get("analysis_snapshot"))
    )
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    if (
        review_input.workspace_ref != snapshot.workspace_ref
        or review_input.base_commit != snapshot.base_commit
        or review_input.workspace_diff_digest != snapshot.workspace_diff_digest
        or state.get("workspace_ref") != review_input.workspace_ref
        or state.get("base_commit") != review_input.base_commit
    ):
        raise ValueError("coding_review_binding_mismatch")
    return {
        "review_snapshot": snapshot,
        "review_input": review_input,
        "review_tasks": build_review_tasks(),
        "review_results": [],
        "review_report": None,
        "review_status": None,
    }


def route_review_workers(state: Mapping[str, object]) -> list[Send] | str:
    """Fan out each unfinished canonical task exactly once to the review worker."""

    snapshot = CodingAnalysisSnapshot.model_validate(state.get("review_snapshot"))
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    tasks = tuple(CodingReviewTask.model_validate(task) for task in state.get("review_tasks", ()))
    if tasks != build_review_tasks():
        raise ValueError("coding_review_contract_invalid")
    completed = {
        CodingReviewerResult.model_validate(result).task_id
        for result in state.get("review_results", ())
    }
    pending = tuple(task for task in tasks if task.task_id not in completed)
    if not pending:
        return "join_review"
    return [
        Send(
            "review_workspace",
            {
                "messages": _review_worker_messages(task, snapshot, review_input),
                "coding_repo_id": state["coding_repo_id"],
                "workspace_ref": review_input.workspace_ref,
                "base_commit": review_input.base_commit,
                "analysis_snapshot": snapshot,
                "review_input": review_input,
                "review_task": task,
                "provider_search_profile": "none",
            },
        )
        for task in pending
    ]


async def review_workspace(
    state: Mapping[str, object],
    runtime: Runtime[AssistantRunContext] | None,
    *,
    review_agent: Any | None = None,
    config: RunnableConfig | None = None,
) -> dict[str, object]:
    """Run one worker and bind all output to its task and frozen final snapshot."""

    task = CodingReviewTask.model_validate(state.get("review_task"))
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    snapshot = CodingAnalysisSnapshot.model_validate(state.get("analysis_snapshot"))
    if (
        snapshot.workspace_ref != review_input.workspace_ref
        or snapshot.base_commit != review_input.base_commit
        or snapshot.workspace_diff_digest != review_input.workspace_diff_digest
    ):
        return {"review_results": [_review_failure_result(task, review_input)]}
    try:
        if review_agent is None:
            raw_result = state.get("structured_response")
        else:
            response = await review_agent.ainvoke(
                dict(state),
                config=config,
                context=runtime.context if runtime is not None else None,
            )
            raw_result = response.get("structured_response")
        if isinstance(raw_result, BaseModel):
            raw_result = raw_result.model_dump(mode="json")
        if not isinstance(raw_result, Mapping):
            raise ValueError("coding_review_contract_invalid")
        normalized = _normalize_review_result(task, review_input, raw_result)
    except Exception:
        normalized = _review_failure_result(task, review_input)
    return {"review_results": [normalized]}


def join_review(state: Mapping[str, object]) -> dict[str, object]:
    """Canonicalize the completed final-review workers without mutating the snapshot."""

    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    results = merge_review_results(
        (),
        tuple(
            CodingReviewerResult.model_validate(result)
            for result in state.get("review_results", ())
        ),
    )
    if {result.task_id for result in results} != set(REVIEW_TASK_IDS):
        return {"review_results": results}
    report = canonicalize_review_report(review_input, results)
    return {
        "review_results": results,
        "review_report": report,
        "review_status": report.status,
    }


def create_coding_review_graph(
    model: Any | None = None,
    workspace_service: CodingWorkspaceService | None = None,
    *,
    review_agent: Any | None = None,
):
    """Compile a fresh, read-only three-worker final-review subgraph."""

    if review_agent is None:
        if model is None or workspace_service is None:
            raise ValueError("coding_review_graph_requires_model_and_workspace")
        review_model = coding_analysis_model_view(model)
        review_agent = create_agent(
            model=review_model,
            tools=build_coding_review_tools(workspace_service),
            system_prompt=_CODING_REVIEW_PROMPT,
            response_format=CodingReviewResponse,
            state_schema=CodingReviewWorkerState,
            context_schema=AssistantRunContext,
            middleware=[
                ModelCallLimitMiddleware(run_limit=1, exit_behavior="error"),
                ToolCallLimitMiddleware(run_limit=8, exit_behavior="error"),
            ],
            name="AssistantCodingReviewAgent",
        )

    async def review_workspace_node(
        state: CodingReviewWorkerState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        return await review_workspace(
            state,
            runtime,
            review_agent=review_agent,
            config=config,
        )

    builder = StateGraph(CodingReviewGraphState, context_schema=AssistantRunContext)
    builder.add_node("prepare_review_tasks", prepare_review_tasks)
    builder.add_node(
        "review_workspace",
        review_workspace_node,
        input_schema=CodingReviewWorkerState,
    )
    builder.add_node("join_review", join_review)
    builder.add_edge(START, "prepare_review_tasks")
    builder.add_conditional_edges(
        "prepare_review_tasks",
        route_review_workers,
        ["review_workspace", "join_review"],
    )
    builder.add_edge("review_workspace", "join_review")
    builder.add_edge("join_review", END)
    return builder.compile(name="AssistantCodingReviewGraph")


def _review_worker_messages(
    task: CodingReviewTask,
    snapshot: CodingAnalysisSnapshot,
    review_input: CodingReviewInput,
) -> list[HumanMessage]:
    context = {
        "task_id": task.task_id,
        "objective": task.objective,
        "snapshot_ref": snapshot.snapshot_ref,
        "tree_digest": snapshot.tree_digest,
        "workspace_ref": review_input.workspace_ref,
        "base_commit": review_input.base_commit,
        "patch_digest": review_input.patch_digest,
        "workspace_diff_digest": review_input.workspace_diff_digest,
        "instruction": "Review only the immutable final snapshot. Return structured findings.",
    }
    return [HumanMessage(content=_canonical_json(context))]


def _normalize_review_result(
    task: CodingReviewTask,
    review_input: CodingReviewInput,
    raw_result: Mapping[str, object],
) -> CodingReviewerResult:
    raw = CodingReviewResponse.model_validate(raw_result)
    findings: list[CodingReviewFinding] = []
    for raw_finding in raw.findings:
        evidence = tuple(
            CodingReviewEvidence(
                path=item.path,
                line=item.line,
                excerpt=item.excerpt,
                evidence_digest=_canonical_digest(
                    {"path": item.path, "line": item.line, "excerpt": item.excerpt}
                ),
            )
            for item in raw_finding.evidence
        )
        semantic_payload = {
            "severity": raw_finding.severity,
            "category": raw_finding.category,
            "summary": raw_finding.summary,
        }
        evidence_payload = [
            item.model_dump(mode="json", exclude={"evidence_digest"})
            for item in evidence
        ]
        findings.append(
            CodingReviewFinding(
                finding_id=_canonical_digest(
                    {"task_id": task.task_id, **semantic_payload, "evidence": evidence_payload}
                ),
                task_id=task.task_id,
                **semantic_payload,
                evidence=evidence,
                semantic_key=_canonical_digest(semantic_payload),
            )
        )
    payload: dict[str, object] = {
        "task_id": task.task_id,
        "workspace_ref": review_input.workspace_ref,
        "base_commit": review_input.base_commit,
        "patch_digest": review_input.patch_digest,
        "workspace_diff_digest": review_input.workspace_diff_digest,
        "status": raw.status,
        "findings": tuple(sorted(findings, key=lambda item: item.finding_id)),
        "error_code": raw.error_code,
    }
    if raw.status != "succeeded":
        payload["findings"] = ()
    digest_payload = {
        **payload,
        "findings": [
            item.model_dump(mode="json")
            for item in payload["findings"]
        ],
    }
    return CodingReviewerResult(
        **payload,
        output_digest=_canonical_digest(digest_payload),
    )


def _review_failure_result(
    task: CodingReviewTask,
    review_input: CodingReviewInput,
) -> CodingReviewerResult:
    return _normalize_review_result(
        task,
        review_input,
        {
            "status": "failed",
            "findings": (),
            "error_code": "coding_review_task_failed",
        },
    )


def canonicalize_review_report(
    review_input: CodingReviewInput,
    results: Sequence[CodingReviewerResult],
) -> CodingReviewReport:
    """Validate the fixed reviewer inventory and return its canonical report."""

    by_task_id: dict[str, CodingReviewerResult] = {}
    for result in results:
        if result.task_id not in REVIEW_TASK_IDS:
            raise ValueError("coding_review_unknown_task")
        if result.task_id in by_task_id:
            raise ValueError("coding_review_duplicate_task")
        _validate_result(result, review_input)
        by_task_id[result.task_id] = result

    missing = [task_id for task_id in REVIEW_TASK_IDS if task_id not in by_task_id]
    if missing:
        raise ValueError("coding_review_missing_task")

    ordered_results = tuple(by_task_id[task_id] for task_id in REVIEW_TASK_IDS)
    findings = _canonical_findings(ordered_results)
    status = _report_status(ordered_results, findings)
    unsigned = CodingReviewReport(
        status=status,
        workspace_ref=review_input.workspace_ref,
        base_commit=review_input.base_commit,
        patch_digest=review_input.patch_digest,
        workspace_diff_digest=review_input.workspace_diff_digest,
        results=ordered_results,
        findings=findings,
        report_digest="0" * 64,
    )
    payload = unsigned.model_dump(mode="json", exclude={"report_digest"})
    signed_payload = {**payload, "report_digest": "0" * 64}
    if len(_canonical_json(signed_payload)) > MAX_REVIEW_REPORT_JSON_CHARS:
        raise ValueError("coding_review_report_limit_exceeded")
    return unsigned.model_copy(update={"report_digest": _canonical_digest(payload)})


def _validate_result(result: CodingReviewerResult, review_input: CodingReviewInput) -> None:
    if (
        result.workspace_ref != review_input.workspace_ref
        or result.base_commit != review_input.base_commit
        or result.patch_digest != review_input.patch_digest
        or result.workspace_diff_digest != review_input.workspace_diff_digest
    ):
        raise ValueError("coding_review_binding_mismatch")
    payload = result.model_dump(mode="json", exclude={"output_digest"})
    if result.output_digest != _canonical_digest(payload):
        raise ValueError("coding_review_contract_invalid")
    signed_payload = result.model_dump(mode="json")
    if len(_canonical_json(signed_payload)) > MAX_REVIEW_RESULT_JSON_CHARS:
        raise ValueError("coding_review_result_limit_exceeded")
    for finding in result.findings:
        if finding.task_id != result.task_id:
            raise ValueError("coding_review_finding_task_mismatch")


def _canonical_findings(
    results: Sequence[CodingReviewerResult],
) -> tuple[CodingReviewFinding, ...]:
    seen: set[tuple[str, str]] = set()
    selected: list[CodingReviewFinding] = []
    for result in results:
        for finding in result.findings:
            evidence_key = _canonical_json(
                [item.model_dump(mode="json") for item in finding.evidence]
            )
            deduplication_key = (evidence_key, finding.semantic_key)
            if deduplication_key in seen:
                continue
            seen.add(deduplication_key)
            selected.append(finding)
    return tuple(sorted(selected, key=_finding_sort_key))


def _finding_sort_key(finding: CodingReviewFinding) -> tuple[int, str, str, int, str]:
    first_evidence = min(
        finding.evidence,
        key=lambda item: (item.path, item.line, item.evidence_digest),
    )
    return (
        _SEVERITY_ORDER[finding.severity],
        finding.task_id,
        first_evidence.path,
        first_evidence.line,
        finding.finding_id,
    )


def _report_status(
    results: Sequence[CodingReviewerResult],
    findings: Sequence[CodingReviewFinding],
) -> str:
    if any(result.status != "succeeded" for result in results):
        return "unavailable"
    return "findings" if findings else "clean"


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CodingReviewResponse",
    "MAX_REVIEW_REPORT_JSON_CHARS",
    "MAX_REVIEW_RESULT_JSON_CHARS",
    "REVIEW_READ_TOOL_NAMES",
    "REVIEW_TASK_IDS",
    "build_coding_review_tools",
    "build_review_tasks",
    "canonicalize_review_report",
    "create_coding_review_graph",
    "join_review",
    "merge_review_results",
    "prepare_review_tasks",
    "review_workspace",
    "route_review_workers",
]
