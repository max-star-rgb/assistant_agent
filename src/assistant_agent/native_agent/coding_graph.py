"""Sequential native graph for governed AI coding patch approval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
    ToolRetryMiddleware,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.errors import NodeError
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Overwrite, RetryPolicy, Send, interrupt
from pydantic import BaseModel

from assistant_agent.coding.analysis import (
    ANALYSIS_TASK_IDS,
    build_analysis_failure_result,
    build_analysis_tasks,
    is_transient_analysis_failure,
    join_analysis_results,
    normalize_analysis_result,
    render_analysis_context,
)
from assistant_agent.coding.models import (
    CodingAnalysisSnapshot,
    CodingAnalysisResult,
    CodingAnalysisTask,
    CodingApprovalDecision,
    CodingMergeApprovalDecision,
    CodingPatchValidation,
    CodingReviewInput,
    CodingReviewReport,
    CodingReviewerResult,
    CodingReviewTask,
    CodingRepairAttempt,
    CodingRepairFailureEvidence,
    CodingTerminalResult,
)
from assistant_agent.coding.repair import (
    MAX_REPAIR_ROUNDS,
    ensure_repair_progress,
    repair_interrupt_payload,
    render_repair_context,
    select_repairable_failure,
    validate_repair_approval,
)
from assistant_agent.coding.review import (
    build_review_tasks,
    build_legacy_review_tasks,
    canonicalize_review_report,
    create_coding_review_graph,
)
from assistant_agent.coding.dependencies import (
    build_dependency_plan,
    dependency_interrupt_payload,
    validate_dependency_approval,
)
from assistant_agent.coding.credentials import (
    build_credential_request,
    credential_interrupt_payload,
    validate_credential_approval,
)
from assistant_agent.coding.artifacts import (
    artifact_interrupt_payload,
    build_artifact_ingress_plan,
    validate_artifact_approval,
)
from assistant_agent.coding.integration import CodingIntegrationService
from assistant_agent.coding.validation import CodingValidationService
from assistant_agent.coding.tools import build_coding_analysis_tools
from assistant_agent.coding.workspace import CodingWorkspaceError, CodingWorkspaceService
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.native_agent.coding_phase import (
    CodingAnalysisPhaseMiddleware,
    coding_analysis_response_format,
)
from assistant_agent.native_agent.providers import (
    coding_analysis_model_settings,
    coding_analysis_model_view,
)
from assistant_agent.native_agent.state import CodingAnalysisWorkerState, CodingState


_CODING_PROMPT = (
    "你是隔离 worktree 中的代码修改 Agent。先用只读 coding_repo_* 工具检查必要上下文，"
    "然后调用 coding_propose_patch 提交一份完整 unified diff。不要假设存在 shell、测试、commit、"
    "merge、push、删除或直接写文件能力；proposal 通过确定性校验后必须等待用户审批。"
)

_CODING_ANALYSIS_PROMPT = (
    "你是代码仓库的只读分析 Agent。只能使用已提供的 snapshot-bound coding_repo_* "
    "只读工具，并严格返回结构化分析结果。分析是 advisory evidence，不得生成 patch、"
    "执行命令、访问网络、申请凭据、修改文件或改变治理策略。"
)
_MAX_ANALYSIS_REQUEST_CHARS = 8_000
_MAX_ANALYSIS_REQUEST_BYTES = 16_000
_MAX_ANALYSIS_ATTEMPTS = 3


def build_coding_graph(
    model: Any,
    tools: list[BaseTool],
    workspace_service: CodingWorkspaceService,
    validation_service: CodingValidationService | None = None,
    integration_service: CodingIntegrationService | None = None,
    *,
    model_call_limit: int = 8,
    tool_call_limit: int = 8,
    inspect_agent: Any | None = None,
    analysis_agent: Any | None = None,
    review_graph: Any | None = None,
    checkpointer: Any | None = None,
):
    """Build the deterministic inspect, approve, and apply sequence."""

    validation_service = validation_service or CodingValidationService(workspace_service)
    integration_service = integration_service or CodingIntegrationService(workspace_service)
    if inspect_agent is None:
        read_names = [
            item.name for item in tools if (item.metadata or {}).get("effect") == "read"
        ]
        middleware: list[Any] = [
            ModelCallLimitMiddleware(run_limit=model_call_limit, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=tool_call_limit, exit_behavior="error"),
        ]
        if read_names:
            middleware.append(
                ToolRetryMiddleware(
                    max_retries=2,
                    tools=read_names,
                    initial_delay=0,
                    backoff_factor=0,
                    jitter=False,
                )
            )
        inspect_agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=_CODING_PROMPT,
            state_schema=CodingState,
            context_schema=AssistantRunContext,
            middleware=middleware,
            name="AssistantCodingInspectAgent",
        )
    if analysis_agent is None:
        analysis_model = coding_analysis_model_view(model)
        analysis_tools = build_coding_analysis_tools(workspace_service)
        analysis_read_names = [item.name for item in analysis_tools]
        analysis_middleware: list[Any] = [
            CodingAnalysisPhaseMiddleware(
                coding_analysis_model_settings(analysis_model)
            ),
            ModelCallLimitMiddleware(run_limit=model_call_limit, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=tool_call_limit, exit_behavior="error"),
        ]
        if analysis_read_names:
            analysis_middleware.append(
                ToolRetryMiddleware(
                    max_retries=2,
                    tools=analysis_read_names,
                    retry_on=is_transient_analysis_failure,
                    on_failure="error",
                    initial_delay=0,
                    backoff_factor=0,
                    jitter=False,
                )
            )
        analysis_agent = create_agent(
            model=analysis_model,
            tools=analysis_tools,
            system_prompt=_CODING_ANALYSIS_PROMPT,
            response_format=coding_analysis_response_format(),
            state_schema=CodingAnalysisWorkerState,
            context_schema=AssistantRunContext,
            middleware=analysis_middleware,
            name="AssistantCodingAnalysisAgent",
        )
    if review_graph is None:
        review_graph = create_coding_review_graph(model, workspace_service)

    def resolve_workspace_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        if state.get("coding_result") is not None:
            return {}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
        except CodingWorkspaceError as exc:
            return {
                "coding_result": CodingTerminalResult(
                    status=("unconfigured" if exc.code == "workspace_not_allowed" else "failed"),
                    error_code=exc.code,
                )
            }
        return {
            "coding_repo_id": workspace.repo_id,
            "workspace_ref": workspace.workspace_ref,
            "base_commit": workspace.base_commit,
        }

    def prepare_analysis_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        workspace = _resolve_workspace(state, runtime, config, workspace_service)
        if state.get("analysis_status") == "pending":
            snapshot = CodingAnalysisSnapshot.model_validate(
                state.get("analysis_snapshot")
            )
            tasks = tuple(
                CodingAnalysisTask.model_validate(task)
                for task in state.get("analysis_tasks", ())
            )
            if tasks != build_analysis_tasks():
                raise ValueError("coding_analysis_contract_invalid")
            workspace_service.resolve_analysis_snapshot(
                snapshot,
                identity=authenticated_user_identity(runtime),
                thread_id=_thread_id(config),
                workspace=workspace,
            )
            if state.get("analysis_results"):
                join_analysis_results(snapshot, state["analysis_results"])
            return {"analysis_snapshot_release_status": "active"}
        snapshot = workspace_service.create_analysis_snapshot(
            workspace,
            identity=authenticated_user_identity(runtime),
            thread_id=_thread_id(config),
        )
        return {
            "analysis_snapshot": snapshot,
            "analysis_tasks": build_analysis_tasks(),
            "analysis_results": [],
            "analysis_status": "pending",
            "analysis_snapshot_release_status": "active",
            "analysis_context_consumed": False,
            "draft_artifact": None,
        }

    async def analyze_workspace_node(
        state: CodingAnalysisWorkerState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        task = CodingAnalysisTask.model_validate(state["analysis_task"])
        snapshot = CodingAnalysisSnapshot.model_validate(state["analysis_snapshot"])
        try:
            result = await analysis_agent.ainvoke(
                dict(state),
                config=config,
                context=runtime.context,
            )
        except BaseException as exc:
            attempt = (
                runtime.execution_info.node_attempt
                if runtime.execution_info is not None
                else 0
            )
            if (
                is_transient_analysis_failure(exc)
                and attempt >= _MAX_ANALYSIS_ATTEMPTS
            ):
                return {
                    "analysis_results": [
                        build_analysis_failure_result(task=task, snapshot=snapshot)
                    ]
                }
            raise
        structured = result.get("structured_response")
        if isinstance(structured, BaseModel):
            raw_result = structured.model_dump(mode="json")
        elif isinstance(structured, Mapping):
            raw_result = dict(structured)
        else:
            raise ValueError("coding_analysis_contract_invalid")
        normalized = normalize_analysis_result(
            task=task,
            snapshot=snapshot,
            raw_result=raw_result,
        )
        return {"analysis_results": [normalized]}

    def analysis_failure_node(
        state: CodingAnalysisWorkerState,
        error: NodeError,
    ) -> Command[str]:
        if not is_transient_analysis_failure(error.error):
            raise error.error
        task = CodingAnalysisTask.model_validate(state["analysis_task"])
        snapshot = CodingAnalysisSnapshot.model_validate(state["analysis_snapshot"])
        return Command(
            update={
                "analysis_results": [
                    build_analysis_failure_result(task=task, snapshot=snapshot)
                ]
            },
            goto="join_analysis",
        )

    def join_analysis_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        workspace = _resolve_workspace(state, runtime, config, workspace_service)
        snapshot = CodingAnalysisSnapshot.model_validate(state["analysis_snapshot"])
        status, results = join_analysis_results(
            snapshot,
            state.get("analysis_results", ()),
        )
        if {result.task_id for result in results} != set(ANALYSIS_TASK_IDS):
            return {
                "analysis_results": list(results),
                "analysis_status": "pending",
                "analysis_snapshot_release_status": "active",
            }
        release_status = "released"
        try:
            workspace_service.release_analysis_snapshot(
                snapshot,
                identity=authenticated_user_identity(runtime),
                thread_id=_thread_id(config),
                workspace=workspace,
            )
        except CodingWorkspaceError as exc:
            if exc.code != "coding_analysis_snapshot_failed":
                raise
            release_status = "cleanup_pending"
        return {
            "analysis_results": list(results),
            "analysis_status": status,
            "analysis_snapshot_release_status": release_status,
        }

    async def inspect_and_draft_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        if state.get("coding_result") is not None:
            return {}
        _resolve_workspace(state, runtime, config, workspace_service)
        call_state = dict(state)
        call_messages = list(state.get("messages", ()))
        analysis_context_added = False
        if state.get("repair_status") == "active":
            repair_model_calls = int(state.get("repair_model_calls", 0))
            if repair_model_calls < 1 or repair_model_calls > MAX_REPAIR_ROUNDS:
                return {
                    "repair_status": "no_progress",
                    "coding_result": _failed(
                        state,
                        "coding_repair_no_progress",
                        repair_status="no_progress",
                    ),
                }
            evidence = state.get("repair_failure_evidence")
            if evidence is None:
                return {
                    "coding_result": _failed(
                        state,
                        "coding_repair_evidence_required",
                    )
                }
            try:
                normalized_evidence = CodingRepairFailureEvidence.model_validate(
                    evidence
                )
            except Exception:
                return {
                    "coding_result": _failed(
                        state,
                        "coding_repair_evidence_required",
                    )
                }
            call_messages.append(
                HumanMessage(
                    content=render_repair_context(
                        normalized_evidence,
                        int(state.get("repair_round", 0)),
                    )
                )
            )
        elif (
            state.get("analysis_status") in {"completed", "partial", "unavailable"}
            and not state.get("analysis_context_consumed", False)
        ):
            call_messages.append(
                HumanMessage(
                    content=render_analysis_context(
                        state["analysis_status"],
                        state.get("analysis_results", ()),
                    )
                )
            )
            analysis_context_added = True
        call_state["messages"] = call_messages
        before = len(call_messages)
        result = await inspect_agent.ainvoke(call_state, config=config)
        new_messages = list(result.get("messages", ()))[before:]
        artifact = None
        for message in reversed(new_messages):
            if (
                isinstance(message, ToolMessage)
                and message.name == "coding_propose_patch"
                and isinstance(message.artifact, dict)
            ):
                artifact = message.artifact
                break
        update: dict[str, object] = {
            "messages": new_messages,
            "draft_artifact": artifact,
        }
        if analysis_context_added:
            update["analysis_context_consumed"] = True
        return update

    def validate_proposal_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        if state.get("coding_result") is not None:
            return {}
        _resolve_workspace(state, runtime, config, workspace_service)
        artifact = state.get("draft_artifact")
        if not isinstance(artifact, dict):
            if state.get("repair_status") == "active":
                return {
                    "repair_status": "no_progress",
                    "coding_result": _failed(
                        state,
                        "coding_repair_no_progress",
                        repair_status="no_progress",
                    ),
                }
            return {
                "coding_result": CodingTerminalResult(
                    status="failed",
                    workspace_ref=state.get("workspace_ref"),
                    base_commit=state.get("base_commit"),
                    error_code="patch_invalid",
                )
            }
        try:
            validation = CodingPatchValidation.model_validate(artifact)
        except Exception:
            if state.get("repair_status") == "active":
                return {
                    "repair_status": "no_progress",
                    "coding_result": _failed(
                        state,
                        "coding_repair_no_progress",
                        repair_status="no_progress",
                    ),
                }
            return {
                "coding_result": CodingTerminalResult(
                    status="failed",
                    workspace_ref=state.get("workspace_ref"),
                    base_commit=state.get("base_commit"),
                    error_code="patch_invalid",
                )
            }
        update: dict[str, object] = {
            "proposal": validation.proposal,
            "validation": validation,
            "approval_status": "pending",
            **_reset_review_state(),
        }
        if state.get("repair_status") != "active":
            update.update(approval_origin="model", format_round=0)
            return update
        try:
            if validation.proposal.patch_digest in state.get(
                "repair_proposal_digests", ()
            ):
                raise ValueError("coding_repair_no_progress")
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            if workspace.workspace_ref != state.get("workspace_ref"):
                raise CodingWorkspaceError("workspace_identity_mismatch")
            context = workspace_service.preview_repair_patch(
                workspace,
                validation,
                int(state.get("repair_round", 0)),
            )
            history = tuple(
                CodingRepairAttempt.model_validate(item)
                for item in state.get("repair_history", ())
            )
            ensure_repair_progress(context, validation.proposal, history)
        except ValueError:
            return {
                "proposal": validation.proposal,
                "validation": validation,
                "approval_status": None,
                "approval_origin": "repair",
                "repair_approval_context": None,
                "repair_status": "no_progress",
                "coding_result": _failed(
                    state,
                    "coding_repair_no_progress",
                    repair_status="no_progress",
                ),
            }
        except CodingWorkspaceError as exc:
            return {
                "proposal": validation.proposal,
                "validation": validation,
                "approval_status": None,
                "approval_origin": "repair",
                "repair_approval_context": None,
                "coding_result": _failed(state, exc.code),
            }
        update.update(
            approval_origin="repair",
            repair_approval_context=context,
            repair_proposal_digests=[validation.proposal.patch_digest],
        )
        return update

    def prepare_repair_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        _resolve_workspace(state, runtime, config, workspace_service)
        evidence = state.get("repair_failure_evidence")
        try:
            normalized_evidence = CodingRepairFailureEvidence.model_validate(evidence)
        except Exception:
            return {
                "coding_result": _failed(
                    state,
                    "coding_repair_evidence_required",
                ),
            }
        return {
            "repair_round": int(state.get("repair_round", 0)) + 1,
            "repair_status": "active",
            "repair_failure_evidence": normalized_evidence,
            "draft_artifact": None,
            "proposal": None,
            "validation": None,
            "approval_status": None,
            "approval_origin": "repair",
            "applied_result": None,
            "dependency_plan": None,
            "dependency_approval_status": None,
            "credential_request": None,
            "credential_approval_status": None,
            "artifact_ingress_plan": None,
            "artifact_approval_status": None,
            "format_round": int(state.get("format_round", 0)),
            "integration_required": False,
            "commit_result": None,
            "merge_preview": None,
            "merge_result": None,
            "repair_approval_context": None,
            **_reset_review_state(),
            "coding_result": None,
        }

    def consume_repair_budget_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        """Persist one active-repair model-call attempt before invoking the model."""
        if state.get("coding_result") is not None:
            return {}
        _resolve_workspace(state, runtime, config, workspace_service)
        if state.get("repair_status") != "active":
            return {
                "coding_result": _failed(
                    state,
                    "coding_repair_evidence_required",
                )
            }
        repair_model_calls = int(state.get("repair_model_calls", 0))
        if repair_model_calls >= MAX_REPAIR_ROUNDS:
            return {
                "repair_status": "no_progress",
                "coding_result": _failed(
                    state,
                    "coding_repair_no_progress",
                    repair_status="no_progress",
                ),
            }
        return {"repair_model_calls": repair_model_calls + 1}

    def approval_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[
        Literal[
            "consume_repair_budget",
            "inspect_and_draft",
            "apply_patch",
            "summarize",
        ]
    ]:
        _resolve_workspace(state, runtime, config, workspace_service)
        validation = state.get("validation")
        if validation is None:
            return Command(goto="summarize")
        proposal = validation.proposal
        if state.get("approval_origin") == "repair":
            context = state.get("repair_approval_context")
            if context is None:
                return Command(
                    update={
                        "coding_result": _failed(
                            state,
                            "approval_digest_mismatch",
                        )
                    },
                    goto="summarize",
                )
            raw = interrupt(
                repair_interrupt_payload(
                    context,
                    workspace_ref=str(state.get("workspace_ref", "")),
                    base_commit=proposal.base_commit,
                    changed_paths=proposal.changed_paths,
                    summary=proposal.summary,
                    diff_preview=validation.diff_preview,
                )
            )
            try:
                decision_kind = validate_repair_approval(context, raw)
                workspace = _resolve_workspace(
                    state,
                    runtime,
                    config,
                    workspace_service,
                )
                if workspace.workspace_ref != state.get("workspace_ref"):
                    raise CodingWorkspaceError("workspace_identity_mismatch")
                fresh_validation = workspace_service.validate_patch(
                    workspace,
                    proposal.patch,
                    proposal.summary,
                )
                fresh_context = workspace_service.preview_repair_patch(
                    workspace,
                    fresh_validation,
                    int(state.get("repair_round", 0)),
                )
                history = tuple(
                    CodingRepairAttempt.model_validate(item)
                    for item in state.get("repair_history", ())
                )
                ensure_repair_progress(
                    fresh_context,
                    fresh_validation.proposal,
                    history,
                )
                if (
                    fresh_context.patch_digest != context.patch_digest
                    or fresh_context.workspace_diff_digest
                    != context.workspace_diff_digest
                    or fresh_context.candidate_diff_digest
                    != context.candidate_diff_digest
                ):
                    raise ValueError("approval_digest_mismatch")
            except (CodingWorkspaceError, ValueError):
                return Command(
                    update={
                        "coding_result": _failed(
                            state,
                            "approval_digest_mismatch",
                        )
                    },
                    goto="summarize",
                )
            if decision_kind == "approve":
                return Command(
                    update={
                        "proposal": fresh_validation.proposal,
                        "validation": fresh_validation,
                        "repair_approval_context": fresh_context,
                        "approval_status": "approved",
                    },
                    goto="apply_patch",
                )
            if decision_kind == "respond":
                response = str(raw.get("response", "")).strip()
                if not response:
                    return Command(
                        update={
                            "coding_result": _failed(state, "invalid_tool_input")
                        },
                        goto="summarize",
                    )
                if int(state.get("repair_model_calls", 0)) >= MAX_REPAIR_ROUNDS:
                    return Command(
                        update={
                            "repair_status": "no_progress",
                            "coding_result": _failed(
                                state,
                                "coding_repair_no_progress",
                                repair_status="no_progress",
                            ),
                        },
                        goto="summarize",
                    )
                return Command(
                    update={
                        "messages": [HumanMessage(content=response)],
                        "draft_artifact": None,
                        "proposal": None,
                        "validation": None,
                        "approval_status": None,
                        "approval_origin": "repair",
                        "repair_approval_context": None,
                    },
                    goto="consume_repair_budget",
                )
            return Command(
                update={
                    "approval_status": "rejected",
                    "coding_result": CodingTerminalResult(
                        status="rejected",
                        workspace_ref=state.get("workspace_ref"),
                        base_commit=state.get("base_commit"),
                        patch_digest=fresh_validation.proposal.patch_digest,
                        changed_paths=fresh_validation.proposal.changed_paths,
                    ),
                },
                goto="summarize",
            )
        raw = interrupt(
            {
                "action": "coding_patch_apply",
                "workspace_ref": state.get("workspace_ref"),
                "base_commit": proposal.base_commit,
                "patch_digest": proposal.patch_digest,
                "changed_paths": list(proposal.changed_paths),
                "summary": proposal.summary,
                "diff_preview": validation.diff_preview,
                "origin": state.get("approval_origin", "model"),
            }
        )
        try:
            decision = CodingApprovalDecision.model_validate(raw)
        except Exception:
            return Command(
                update={
                    "coding_result": _failed(state, "approval_digest_mismatch"),
                },
                goto="summarize",
            )
        if decision.decision == "approve":
            if decision.patch_digest != proposal.patch_digest:
                return Command(
                    update={"coding_result": _failed(state, "approval_digest_mismatch")},
                    goto="summarize",
                )
            return Command(update={"approval_status": "approved"}, goto="apply_patch")
        if decision.decision == "respond":
            if not (decision.response or "").strip():
                return Command(
                    update={"coding_result": _failed(state, "invalid_tool_input")},
                    goto="summarize",
                )
            if (
                state.get("repair_status") == "active"
                and int(state.get("repair_model_calls", 0)) >= MAX_REPAIR_ROUNDS
            ):
                return Command(
                    update={
                        "repair_status": "no_progress",
                        "coding_result": _failed(
                            state,
                            "coding_repair_no_progress",
                            repair_status="no_progress",
                        ),
                    },
                    goto="summarize",
                )
            active_repair = state.get("repair_status") == "active"
            return Command(
                update={
                    "messages": [HumanMessage(content=decision.response.strip())],
                    "draft_artifact": None,
                    "proposal": None,
                    "validation": None,
                    "approval_status": None,
                    "approval_origin": "repair" if active_repair else "model",
                    "repair_approval_context": None if active_repair else state.get(
                        "repair_approval_context"
                    ),
                },
                goto=("consume_repair_budget" if active_repair else "inspect_and_draft"),
            )
        return Command(
            update={
                "approval_status": "rejected",
                "coding_result": CodingTerminalResult(
                    status="rejected",
                    workspace_ref=state.get("workspace_ref"),
                    base_commit=state.get("base_commit"),
                    patch_digest=proposal.patch_digest,
                    changed_paths=proposal.changed_paths,
                ),
            },
            goto="summarize",
        )

    def apply_patch_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        validation = state.get("validation")
        if validation is None or state.get("approval_status") != "approved":
            return {"coding_result": _failed(state, "approval_required")}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            if workspace.workspace_ref != state.get("workspace_ref"):
                raise CodingWorkspaceError("workspace_identity_mismatch")
            applied = workspace_service.apply_validated_patch(workspace, validation)
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        return {
            "applied_result": applied,
            "approved_changed_paths": list(applied.changed_paths),
            "format_round": (
                int(state.get("format_round", 0)) + 1
                if state.get("approval_origin") == "formatter"
                else int(state.get("format_round", 0))
            ),
        }

    def plan_dependencies_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        applied = state.get("applied_result")
        if applied is None:
            return {"coding_result": _failed(state, "patch_apply_failed")}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None:
                raise CodingWorkspaceError("workspace_not_allowed")
            plan = build_dependency_plan(
                repository,
                workspace.root,
                changed_paths=_approved_changed_paths(state, applied),
            )
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        except ValueError:
            return {
                "coding_result": _failed(state, "dependency_lockfile_invalid"),
                "dependency_plan": None,
                "dependency_approval_status": None,
            }
        return {
            "dependency_plan": plan,
            "dependency_approval_status": (
                "pending" if plan is not None else "not_required"
            ),
        }

    def dependency_approval_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[Literal["plan_credentials", "summarize"]]:
        _resolve_workspace(state, runtime, config, workspace_service)
        plan = state.get("dependency_plan")
        applied = state.get("applied_result")
        if plan is None or applied is None:
            return Command(
                update={
                    "coding_result": _failed(state, "dependency_approval_required")
                },
                goto="summarize",
            )
        raw = interrupt(dependency_interrupt_payload(plan))
        try:
            decision = validate_dependency_approval(plan, raw)
        except ValueError:
            return Command(
                update={
                    "coding_result": _failed(
                        state,
                        "dependency_approval_mismatch",
                    )
                },
                goto="summarize",
            )
        if decision == "reject":
            return Command(
                update={
                    "dependency_approval_status": "rejected",
                    "coding_result": CodingTerminalResult(
                        status="rejected",
                        workspace_ref=state.get("workspace_ref"),
                        base_commit=state.get("base_commit"),
                        patch_digest=applied.patch_digest,
                        changed_paths=_approved_changed_paths(state, applied),
                        error_code="dependency_install_rejected",
                        **_repair_terminal_fields(state),
                    ),
                },
                goto="summarize",
            )
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None:
                raise CodingWorkspaceError("workspace_not_allowed")
            fresh_plan = build_dependency_plan(
                repository,
                workspace.root,
                changed_paths=_approved_changed_paths(state, applied),
            )
        except (CodingWorkspaceError, ValueError):
            fresh_plan = None
        if fresh_plan is None or fresh_plan.plan_digest != plan.plan_digest:
            return Command(
                update={
                    "coding_result": _failed(
                        state,
                        "dependency_approval_mismatch",
                    )
                },
                goto="summarize",
            )
        return Command(
            update={
                "dependency_plan": fresh_plan,
                "dependency_approval_status": "approved",
            },
            goto="plan_credentials",
        )

    def plan_credentials_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        plan = state.get("dependency_plan")
        if plan is None:
            return {
                "credential_request": None,
                "credential_approval_status": "not_required",
            }
        if state.get("dependency_approval_status") != "approved":
            return {
                "coding_result": _failed(state, "dependency_approval_required")
            }
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            dependency = repository.dependency_profile if repository is not None else None
            credential_id = dependency.credential_profile_id if dependency else None
            if credential_id is None:
                return {
                    "credential_request": None,
                    "credential_approval_status": "not_required",
                }
            credential = workspace_service.config.credential_profiles.get(credential_id)
            if credential is None:
                raise ValueError("credential_broker_unconfigured")
            request = build_credential_request(dependency, credential, plan)
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        except ValueError as exc:
            return {"coding_result": _failed(state, str(exc))}
        return {
            "credential_request": request,
            "credential_approval_status": "pending",
        }

    def credential_approval_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[Literal["run_validation", "summarize"]]:
        _resolve_workspace(state, runtime, config, workspace_service)
        request = state.get("credential_request")
        plan = state.get("dependency_plan")
        applied = state.get("applied_result")
        if request is None or plan is None or applied is None:
            return Command(
                update={"coding_result": _failed(state, "credential_approval_required")},
                goto="summarize",
            )
        raw = interrupt(credential_interrupt_payload(request))
        try:
            decision = validate_credential_approval(request, raw)
        except ValueError:
            return Command(
                update={"coding_result": _failed(state, "credential_approval_mismatch")},
                goto="summarize",
            )
        if decision == "reject":
            return Command(
                update={
                    "credential_approval_status": "rejected",
                    "coding_result": CodingTerminalResult(
                        status="rejected",
                        workspace_ref=state.get("workspace_ref"),
                        base_commit=state.get("base_commit"),
                        patch_digest=applied.patch_digest,
                        changed_paths=_approved_changed_paths(state, applied),
                        error_code="credential_lease_rejected",
                        **_repair_terminal_fields(state),
                    ),
                },
                goto="summarize",
            )
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            dependency = repository.dependency_profile if repository is not None else None
            credential_id = dependency.credential_profile_id if dependency else None
            credential = workspace_service.config.credential_profiles.get(credential_id)
            fresh_plan = build_dependency_plan(
                repository,
                workspace.root,
                changed_paths=_approved_changed_paths(state, applied),
            )
            if fresh_plan is None or credential is None or dependency is None:
                raise ValueError("credential_approval_mismatch")
            fresh_request = build_credential_request(dependency, credential, fresh_plan)
        except (CodingWorkspaceError, ValueError, AttributeError):
            fresh_request = None
        if fresh_request is None or fresh_request.request_digest != request.request_digest:
            return Command(
                update={"coding_result": _failed(state, "credential_approval_mismatch")},
                goto="summarize",
            )
        return Command(
            update={
                "dependency_plan": fresh_plan,
                "credential_request": fresh_request,
                "credential_approval_status": "approved",
            },
            goto="plan_artifacts",
        )

    def plan_artifacts_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        applied = state.get("applied_result")
        if applied is None:
            return {"coding_result": _failed(state, "patch_apply_failed")}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None:
                raise CodingWorkspaceError("workspace_not_allowed")
            plan = build_artifact_ingress_plan(
                repository,
                workspace.root,
                changed_paths=_approved_changed_paths(state, applied),
            )
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        except ValueError:
            return {
                "coding_result": _failed(state, "artifact_manifest_invalid"),
                "artifact_ingress_plan": None,
                "artifact_approval_status": None,
            }
        return {
            "artifact_ingress_plan": plan,
            "artifact_approval_status": "pending" if plan is not None else "not_required",
        }

    def artifact_approval_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[Literal["run_validation", "summarize"]]:
        _resolve_workspace(state, runtime, config, workspace_service)
        plan = state.get("artifact_ingress_plan")
        applied = state.get("applied_result")
        if plan is None or applied is None:
            return Command(
                update={"coding_result": _failed(state, "artifact_approval_required")},
                goto="summarize",
            )
        raw = interrupt(artifact_interrupt_payload(plan))
        try:
            decision = validate_artifact_approval(plan, raw)
        except ValueError:
            return Command(
                update={"coding_result": _failed(state, "artifact_approval_mismatch")},
                goto="summarize",
            )
        if decision == "reject":
            return Command(
                update={
                    "artifact_approval_status": "rejected",
                    "coding_result": CodingTerminalResult(
                        status="rejected",
                        workspace_ref=state.get("workspace_ref"),
                        base_commit=state.get("base_commit"),
                        patch_digest=applied.patch_digest,
                        changed_paths=_approved_changed_paths(state, applied),
                        error_code="artifact_ingress_rejected",
                        **_repair_terminal_fields(state),
                    ),
                },
                goto="summarize",
            )
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            fresh_plan = build_artifact_ingress_plan(
                repository,
                workspace.root,
                changed_paths=_approved_changed_paths(state, applied),
            )
        except (CodingWorkspaceError, ValueError, AttributeError):
            fresh_plan = None
        if fresh_plan is None or fresh_plan.plan_digest != plan.plan_digest:
            return Command(
                update={"coding_result": _failed(state, "artifact_approval_mismatch")},
                goto="summarize",
            )
        return Command(
            update={
                "artifact_ingress_plan": fresh_plan,
                "artifact_approval_status": "approved",
            },
            goto="run_validation",
        )

    def run_validation_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        applied = state.get("applied_result")
        if applied is None:
            return {"coding_result": _failed(state, "patch_apply_failed")}
        if (
            state.get("dependency_plan") is not None
            and state.get("dependency_approval_status") != "approved"
        ):
            return {
                "coding_result": _failed(state, "dependency_approval_required")
            }
        if (
            state.get("credential_request") is not None
            and state.get("credential_approval_status") != "approved"
        ):
            return {
                "coding_result": _failed(state, "credential_approval_required")
            }
        if (
            state.get("artifact_ingress_plan") is not None
            and state.get("artifact_approval_status") != "approved"
        ):
            return {"coding_result": _failed(state, "artifact_approval_required")}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None:
                raise CodingWorkspaceError("workspace_not_allowed")
            validation_options: dict[str, object] = {
                "format_round": int(state.get("format_round", 0)),
                "identity": authenticated_user_identity(runtime),
                "thread_id": _thread_id(config),
                "generation": int(state.get("coding_cycle_generation") or 1),
            }
            if state.get("dependency_plan") is not None:
                validation_options["dependency_plan"] = state["dependency_plan"]
            if state.get("credential_request") is not None:
                validation_options["credential_request"] = state["credential_request"]
            if state.get("artifact_ingress_plan") is not None:
                validation_options["artifact_ingress_plan"] = state[
                    "artifact_ingress_plan"
                ]
            result = validation_service.run(
                workspace,
                repository,
                **validation_options,
            )
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        update: dict[str, object] = {
            "verification_evidence": list(result.evidence),
            "validation_snapshot": result.validated_snapshot,
            "validation_binding_digest": result.validation_binding_digest,
            **_reset_review_state(),
        }
        if result.status in {"passed", "failed"}:
            update["last_verification_status"] = result.status
        if result.status == "format_approval_required":
            validation = result.formatter_validation
            if validation is None:
                update["coding_result"] = _failed(state, "patch_invalid")
                return update
            update.update(
                proposal=validation.proposal,
                validation=validation,
                approval_status="pending",
                approval_origin="formatter",
            )
            return update
        all_evidence = (*state.get("verification_evidence", ()), *result.evidence)
        repair_history = _repair_history(state)
        if result.status == "failed":
            current_repair_round = int(state.get("repair_round", 0))
            current_attempt = None
            if state.get("repair_status") == "active":
                context = state.get("repair_approval_context")
                failure = state.get("repair_failure_evidence")
                if context is not None and failure is not None:
                    current_attempt = CodingRepairAttempt(
                        round=current_repair_round,
                        failure_output_digest=failure.output_digest,
                        patch_digest=context.patch_digest,
                        workspace_diff_digest=context.workspace_diff_digest,
                        candidate_diff_digest=context.candidate_diff_digest,
                        status="failed",
                    )
                    update["repair_history"] = [current_attempt]
            terminal_history = (
                (*repair_history, current_attempt)
                if current_attempt is not None
                else repair_history
            )
            eligible_failure = select_repairable_failure(result, 0)
            repairable = select_repairable_failure(result, current_repair_round)
            repair_budget_exhausted = (
                state.get("repair_status") == "active"
                and int(state.get("repair_model_calls", 0)) >= MAX_REPAIR_ROUNDS
            )
            if repairable is not None and not repair_budget_exhausted:
                update.update(
                    repair_failure_evidence=repairable,
                    repair_status="pending",
                )
                return update
            terminal_repair_status = (
                "exhausted"
                if state.get("repair_status") == "active"
                and eligible_failure is not None
                and (
                    current_repair_round >= MAX_REPAIR_ROUNDS
                    or repair_budget_exhausted
                )
                else None
            )
            if terminal_repair_status is not None:
                update["repair_status"] = terminal_repair_status
            elif state.get("repair_status") == "active":
                update.update(
                    repair_status=None,
                    repair_failure_evidence=None,
                    repair_approval_context=None,
                    draft_artifact=None,
                    proposal=None,
                    validation=None,
                    approval_status=None,
                    applied_result=None,
                    dependency_plan=None,
                    dependency_approval_status=None,
                    credential_request=None,
                    credential_approval_status=None,
                    artifact_ingress_plan=None,
                    artifact_approval_status=None,
                    integration_required=False,
                )
            update["coding_result"] = CodingTerminalResult(
                status="failed",
                workspace_ref=state.get("workspace_ref"),
                base_commit=state.get("base_commit"),
                patch_digest=applied.patch_digest,
                changed_paths=_approved_changed_paths(state, applied),
                error_code=result.error_code or "verification_command_failed",
                verification_status="failed",
                verification_evidence=all_evidence,
                repair_status=terminal_repair_status,
                repair_history=terminal_history,
            )
            return update
        terminal_history = repair_history
        terminal_repair_status = None
        if state.get("repair_status") == "active":
            context = state.get("repair_approval_context")
            failure = state.get("repair_failure_evidence")
            if context is not None and failure is not None:
                current_attempt = CodingRepairAttempt(
                    round=int(state.get("repair_round", 0)),
                    failure_output_digest=failure.output_digest,
                    patch_digest=context.patch_digest,
                    workspace_diff_digest=context.workspace_diff_digest,
                    candidate_diff_digest=context.candidate_diff_digest,
                    status="passed",
                )
                update["repair_history"] = [current_attempt]
                terminal_history = (*repair_history, current_attempt)
            terminal_repair_status = "passed"
            update["repair_status"] = terminal_repair_status
            update["repair_failure_evidence"] = None
        update["integration_required"] = repository.integration_enabled
        if repository.code_review_enabled:
            update["review_required"] = True
            return update
        if repository.integration_enabled:
            return update
        update["coding_result"] = CodingTerminalResult(
            status="applied",
            workspace_ref=applied.workspace_ref,
            base_commit=applied.base_commit,
            patch_digest=applied.patch_digest,
            changed_paths=_approved_changed_paths(state, applied),
            verification_status="passed",
            verification_evidence=all_evidence,
            repair_status=terminal_repair_status,
            repair_history=terminal_history,
        )
        return update

    def prepare_review_snapshot_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        applied = state.get("applied_result")
        if (
            state.get("coding_result") is not None
            or not state.get("review_required")
            or state.get("last_verification_status") != "passed"
            or applied is None
        ):
            return {
                "coding_result": _failed(state, "coding_review_binding_mismatch")
            }
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            snapshot = CodingAnalysisSnapshot.model_validate(
                state.get("validation_snapshot")
            )
            workspace_service.validate_analysis_snapshot(
                snapshot,
                identity=authenticated_user_identity(runtime),
                thread_id=_thread_id(config),
                workspace=workspace,
                require_active=True,
            )
            validation_digest = state.get("validation_binding_digest")
            if not isinstance(validation_digest, str):
                raise ValueError("coding_review_binding_mismatch")
        except (CodingWorkspaceError, ValueError):
            return {
                "coding_result": _failed(state, "coding_review_binding_mismatch")
            }
        review_input = CodingReviewInput(
            workspace_ref=workspace.workspace_ref,
            base_commit=workspace.base_commit,
            patch_digest=applied.patch_digest,
            workspace_diff_digest=snapshot.workspace_diff_digest,
            snapshot_materialization_schema_version=(
                snapshot.materialization_schema_version
            ),
            snapshot_created_at=snapshot.created_at,
            snapshot_expires_at=snapshot.expires_at,
            generation=int(state.get("coding_cycle_generation") or 1),
            snapshot_ref=snapshot.snapshot_ref,
            tree_digest=snapshot.tree_digest,
            validation_evidence_digest=validation_digest,
            review_tasks=tuple(task.task_id for task in build_review_tasks()),
        )
        return {
            "review_generation": int(state.get("coding_cycle_generation") or 0),
            "review_snapshot": snapshot,
            "review_snapshot_schema_version": (
                snapshot.materialization_schema_version
            ),
            "review_snapshot_release_status": "active",
            "review_input": review_input,
            "review_tasks": (),
            "review_results": Overwrite([]),
            "review_report": None,
            "review_status": None,
            "review_validation_digest": validation_digest,
            "review_decision_context": None,
            "review_decision": None,
        }

    async def run_code_review_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        try:
            _resolve_workspace(state, runtime, config, workspace_service)
            snapshot = CodingAnalysisSnapshot.model_validate(state.get("review_snapshot"))
            review_input = CodingReviewInput.model_validate(state.get("review_input"))
            projected = {
                "coding_repo_id": state["coding_repo_id"],
                "workspace_ref": review_input.workspace_ref,
                "base_commit": review_input.base_commit,
                "review_snapshot": snapshot,
                "review_input": review_input,
            }
            output = await review_graph.ainvoke(
                projected,
                config=config,
                context=runtime.context,
            )
            tasks = tuple(
                CodingReviewTask.model_validate(item)
                for item in output.get("review_tasks", ())
            )
            results = tuple(
                CodingReviewerResult.model_validate(item)
                for item in output.get("review_results", ())
            )
            report = CodingReviewReport.model_validate(output.get("review_report"))
            canonical_report = canonicalize_review_report(review_input, results)
            if tasks != build_review_tasks() or report != canonical_report:
                raise ValueError("coding_review_contract_invalid")
            decision_context = _review_binding_context(state, report=report)
        except (CodingWorkspaceError, ValueError, TypeError):
            return {"coding_result": _failed(state, "coding_review_binding_mismatch")}
        return {
            "review_tasks": tasks,
            "review_results": list(results),
            "review_report": report,
            "review_status": report.status,
            "review_decision_context": decision_context,
        }

    def coding_review_decision_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[Literal["create_commit", "summarize"]]:
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            if state.get("review_decision") is not None:
                raise ValueError("coding_review_decision_stale")
            decision_context = _review_binding_context(state)
            allow_legacy_schema_omission = (
                state.get("review_report") is not None
                and decision_context["snapshot_materialization_schema_version"]
                == "legacy_v1"
            )
            if not _review_decision_context_matches(
                state.get("review_decision_context"),
                decision_context,
                allow_legacy_schema_omission=allow_legacy_schema_omission,
            ):
                raise ValueError("coding_review_binding_mismatch")
        except (CodingWorkspaceError, ValueError, TypeError):
            return Command(
                update={
                    "coding_result": _failed(
                        state,
                        "coding_review_binding_mismatch",
                    )
                },
                goto="summarize",
            )
        raw = interrupt(
            {
                "action": "coding_review_decision",
                **decision_context,
                "review_status": state.get("review_status"),
                "finding_count": len(state.get("review_report").findings),
                "findings_summary": _review_findings_summary(
                    state.get("review_report").findings
                ),
            }
        )
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            decision_context = _review_binding_context(state)
            allow_legacy_schema_omission = (
                state.get("review_report") is not None
                and decision_context["snapshot_materialization_schema_version"]
                == "legacy_v1"
            )
            if not _review_decision_context_matches(
                state.get("review_decision_context"),
                decision_context,
                allow_legacy_schema_omission=allow_legacy_schema_omission,
            ):
                raise ValueError("coding_review_binding_mismatch")
            decision = _validate_review_decision(
                raw,
                decision_context,
                allow_legacy_schema_omission=allow_legacy_schema_omission,
            )
            snapshot = CodingAnalysisSnapshot.model_validate(state.get("review_snapshot"))
            fresh_snapshot = workspace_service.create_analysis_snapshot(
                workspace,
                identity=authenticated_user_identity(runtime),
                thread_id=_thread_id(config),
            )
            if not _review_snapshot_content_matches(
                snapshot,
                fresh_snapshot,
                expected_schema_version=_expected_review_snapshot_schema_version(
                    state,
                    completed=True,
                ),
            ):
                try:
                    workspace_service.release_analysis_snapshot(
                        fresh_snapshot,
                        identity=authenticated_user_identity(runtime),
                        thread_id=_thread_id(config),
                        workspace=workspace,
                    )
                except CodingWorkspaceError:
                    pass
                raise ValueError("coding_review_binding_mismatch")
            workspace_service.release_analysis_snapshot(
                fresh_snapshot,
                identity=authenticated_user_identity(runtime),
                thread_id=_thread_id(config),
                workspace=workspace,
            )
        except (CodingWorkspaceError, ValueError, TypeError):
            return Command(
                update={
                    "coding_result": _failed(
                        state,
                        "coding_review_binding_mismatch",
                    )
                },
                goto="summarize",
            )
        if decision == "reject":
            return Command(
                update={
                    "review_decision": "rejected",
                    "review_snapshot_release_status": "released",
                    "coding_result": CodingTerminalResult(
                        status="rejected",
                        workspace_ref=state.get("workspace_ref"),
                        base_commit=state.get("base_commit"),
                        patch_digest=state.get("applied_result").patch_digest,
                        changed_paths=_approved_changed_paths(state),
                        error_code="coding_review_rejected",
                        verification_status="passed",
                        verification_evidence=tuple(
                            state.get("verification_evidence", ())
                        ),
                        **_repair_terminal_fields(state),
                    ),
                },
                goto="summarize",
            )
        approval_update: dict[str, object] = {
            "review_decision": "approved",
            "review_snapshot_release_status": "released",
        }
        if state.get("integration_required"):
            return Command(update=approval_update, goto="create_commit")
        applied = state.get("applied_result")
        approval_update["coding_result"] = CodingTerminalResult(
            status="applied",
            workspace_ref=applied.workspace_ref,
            base_commit=applied.base_commit,
            patch_digest=applied.patch_digest,
            changed_paths=_approved_changed_paths(state, applied),
            verification_status="passed",
            verification_evidence=tuple(state.get("verification_evidence", ())),
            **_repair_terminal_fields(state),
        )
        return Command(update=approval_update, goto="summarize")

    def create_commit_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None or not state.get("integration_required"):
                raise CodingWorkspaceError("integration_not_enabled")
            committed = integration_service.create_commit(
                workspace,
                repository,
                changed_paths=tuple(state.get("approved_changed_paths", ())),
                verification_evidence=tuple(state.get("verification_evidence", ())),
                expected_snapshot=CodingAnalysisSnapshot.model_validate(
                    state.get("validation_snapshot")
                ),
                expected_workspace_diff_digest=CodingReviewReport.model_validate(
                    state.get("review_report")
                ).workspace_diff_digest
                if state.get("review_required")
                else CodingAnalysisSnapshot.model_validate(
                    state.get("validation_snapshot")
                ).workspace_diff_digest,
                expected_validation_binding_digest=state.get(
                    "validation_binding_digest"
                ),
                expected_review_report_digest=(
                    CodingReviewReport.model_validate(
                        state.get("review_report")
                    ).report_digest
                    if state.get("review_required")
                    else None
                ),
                identity=authenticated_user_identity(runtime),
                thread_id=_thread_id(config),
            )
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        return {"commit_result": committed}

    def prepare_merge_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        committed = state.get("commit_result")
        if committed is None:
            return {"coding_result": _failed(state, "commit_required")}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None:
                raise CodingWorkspaceError("integration_not_enabled")
            preview = integration_service.prepare_merge(workspace, repository, committed)
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        return {"merge_preview": preview}

    def merge_approval_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[Literal["apply_merge", "summarize"]]:
        _resolve_workspace(state, runtime, config, workspace_service)
        preview = state.get("merge_preview")
        if preview is None:
            return Command(
                update={"coding_result": _failed(state, "merge_preview_required")},
                goto="summarize",
            )
        raw = interrupt(
            {
                "action": "coding_merge_apply",
                "source_commit": preview.source_commit,
                "expected_target_head": preview.expected_target_head,
                "target_branch": preview.target_branch,
                "strategy": preview.strategy,
                "result_commit": preview.result_commit,
                "result_tree": preview.result_tree,
                "merge_preview_digest": preview.merge_preview_digest,
            }
        )
        try:
            decision = CodingMergeApprovalDecision.model_validate(raw)
        except Exception:
            return Command(
                update={"coding_result": _failed(state, "merge_approval_mismatch")},
                goto="summarize",
            )
        if decision.decision == "reject":
            return Command(
                update={
                    "coding_result": CodingTerminalResult(
                        status="rejected",
                        workspace_ref=state.get("workspace_ref"),
                        base_commit=state.get("base_commit"),
                        changed_paths=tuple(state.get("approved_changed_paths", ())),
                        error_code="merge_rejected",
                        verification_status="passed",
                        verification_evidence=tuple(
                            state.get("verification_evidence", ())
                        ),
                        source_commit=preview.source_commit,
                        expected_target_head=preview.expected_target_head,
                        result_commit=preview.result_commit,
                        merge_preview_digest=preview.merge_preview_digest,
                        **_repair_terminal_fields(state),
                    )
                },
                goto="summarize",
            )
        if (
            decision.source_commit != preview.source_commit
            or decision.expected_target_head != preview.expected_target_head
            or decision.merge_preview_digest != preview.merge_preview_digest
        ):
            return Command(
                update={"coding_result": _failed(state, "merge_approval_mismatch")},
                goto="summarize",
            )
        return Command(goto="apply_merge")

    def apply_merge_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
        preview = state.get("merge_preview")
        if preview is None:
            return {"coding_result": _failed(state, "merge_preview_required")}
        try:
            workspace = _resolve_workspace(state, runtime, config, workspace_service)
            repository = workspace_service.config.repositories.get(workspace.repo_id)
            if repository is None:
                raise CodingWorkspaceError("integration_not_enabled")
            merged = integration_service.apply_merge(workspace, repository, preview)
        except CodingWorkspaceError as exc:
            return {"coding_result": _failed(state, exc.code)}
        return {
            "merge_result": merged,
            "coding_result": CodingTerminalResult(
                status="merged",
                workspace_ref=state.get("workspace_ref"),
                base_commit=state.get("base_commit"),
                changed_paths=tuple(state.get("approved_changed_paths", ())),
                verification_status="passed",
                verification_evidence=tuple(state.get("verification_evidence", ())),
                source_commit=merged.source_commit,
                expected_target_head=merged.previous_target_head,
                result_commit=merged.result_commit,
                merge_preview_digest=merged.merge_preview_digest,
                **_repair_terminal_fields(state),
            ),
        }

    def summarize_node(state: CodingState) -> dict[str, object]:
        result = state.get("coding_result") or _failed(state, "patch_invalid")
        return {
            "coding_result": result,
            "review_snapshot": None,
            "review_snapshot_release_status": None,
            "review_input": None,
            "review_tasks": (),
            "review_results": Overwrite([]),
            "review_decision_context": None,
            "messages": [
                AIMessage(
                    content=(
                        f"Coding workspace result: {result.status}. "
                        f"Changed files: {len(result.changed_paths)}."
                    )
                )
            ],
        }

    def after_resolve(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("repair_status") == "active":
            return "consume_repair_budget"
        if state.get("analysis_status") in {"completed", "partial", "unavailable"}:
            return "inspect_and_draft"
        if state.get("analysis_status") == "pending":
            return "prepare_analysis"
        repository = workspace_service.config.repositories.get(
            str(state.get("coding_repo_id", ""))
        )
        if repository is not None and repository.parallel_analysis_enabled:
            return "prepare_analysis"
        return "inspect_and_draft"

    def after_validation(state: CodingState) -> str:
        return "summarize" if state.get("coding_result") is not None else "approval"

    def after_run_validation(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("repair_status") == "pending":
            return "prepare_repair"
        if state.get("approval_status") == "pending":
            return "approval"
        if state.get("review_required"):
            return "prepare_review_snapshot"
        if state.get("integration_required"):
            return "create_commit"
        return "summarize"

    def after_prepare_review(state: CodingState) -> str:
        return "summarize" if state.get("coding_result") is not None else "run_code_review"

    def after_code_review(state: CodingState) -> str:
        return (
            "summarize"
            if state.get("coding_result") is not None
            else "coding_review_decision"
        )

    def after_dependency_plan(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("dependency_plan") is not None:
            return "dependency_approval"
        return "plan_credentials"

    def after_credential_plan(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("credential_request") is not None:
            return "credential_approval"
        return "plan_artifacts"

    def after_artifact_plan(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("artifact_ingress_plan") is not None:
            return "artifact_approval"
        return "run_validation"

    def after_integration_step(state: CodingState) -> str:
        return "summarize" if state.get("coding_result") is not None else "prepare_merge"

    def after_merge_preview(state: CodingState) -> str:
        return "summarize" if state.get("coding_result") is not None else "merge_approval"

    builder = StateGraph(CodingState, context_schema=AssistantRunContext)
    builder.add_node("begin_coding_cycle", begin_coding_cycle_node)
    builder.add_node("resolve_workspace", resolve_workspace_node)
    builder.add_node("prepare_analysis", prepare_analysis_node)
    builder.add_node(
        "analyze_workspace",
        analyze_workspace_node,
        input_schema=CodingAnalysisWorkerState,
        retry_policy=RetryPolicy(
            initial_interval=0,
            backoff_factor=0,
            max_attempts=_MAX_ANALYSIS_ATTEMPTS,
            jitter=False,
            retry_on=is_transient_analysis_failure,
        ),
        error_handler=analysis_failure_node,
    )
    builder.add_node("join_analysis", join_analysis_node)
    builder.add_node("inspect_and_draft", inspect_and_draft_node)
    builder.add_node("validate_proposal", validate_proposal_node)
    builder.add_node("prepare_repair", prepare_repair_node)
    builder.add_node("consume_repair_budget", consume_repair_budget_node)
    builder.add_node("approval", approval_node)
    builder.add_node("apply_patch", apply_patch_node)
    builder.add_node("plan_dependencies", plan_dependencies_node)
    builder.add_node("dependency_approval", dependency_approval_node)
    builder.add_node("plan_credentials", plan_credentials_node)
    builder.add_node("credential_approval", credential_approval_node)
    builder.add_node("plan_artifacts", plan_artifacts_node)
    builder.add_node("artifact_approval", artifact_approval_node)
    builder.add_node("run_validation", run_validation_node)
    builder.add_node("prepare_review_snapshot", prepare_review_snapshot_node)
    builder.add_node("run_code_review", run_code_review_node)
    builder.add_node("coding_review_decision", coding_review_decision_node)
    builder.add_node("create_commit", create_commit_node)
    builder.add_node("prepare_merge", prepare_merge_node)
    builder.add_node("merge_approval", merge_approval_node)
    builder.add_node("apply_merge", apply_merge_node)
    builder.add_node("summarize", summarize_node)
    builder.add_edge(START, "begin_coding_cycle")
    builder.add_edge("begin_coding_cycle", "resolve_workspace")
    builder.add_conditional_edges(
        "resolve_workspace",
        after_resolve,
        [
            "consume_repair_budget",
            "inspect_and_draft",
            "prepare_analysis",
            "summarize",
        ],
    )
    builder.add_conditional_edges(
        "prepare_analysis",
        route_analysis_workers,
        ["analyze_workspace", "join_analysis"],
    )
    builder.add_edge("analyze_workspace", "join_analysis")
    builder.add_edge("join_analysis", "inspect_and_draft")
    builder.add_edge("inspect_and_draft", "validate_proposal")
    builder.add_conditional_edges(
        "validate_proposal",
        after_validation,
        ["approval", "summarize"],
    )
    builder.add_edge("prepare_repair", "consume_repair_budget")
    builder.add_edge("consume_repair_budget", "inspect_and_draft")
    builder.add_edge("apply_patch", "plan_dependencies")
    builder.add_conditional_edges(
        "plan_dependencies",
        after_dependency_plan,
        ["dependency_approval", "plan_credentials", "summarize"],
    )
    builder.add_conditional_edges(
        "plan_credentials",
        after_credential_plan,
        ["credential_approval", "plan_artifacts", "summarize"],
    )
    builder.add_conditional_edges(
        "plan_artifacts",
        after_artifact_plan,
        ["artifact_approval", "run_validation", "summarize"],
    )
    builder.add_conditional_edges(
        "run_validation",
        after_run_validation,
        {
            "approval": "approval",
            "create_commit": "create_commit",
            "prepare_repair": "prepare_repair",
            "prepare_review_snapshot": "prepare_review_snapshot",
            "summarize": "summarize",
        },
    )
    builder.add_conditional_edges(
        "prepare_review_snapshot",
        after_prepare_review,
        {"run_code_review": "run_code_review", "summarize": "summarize"},
    )
    builder.add_conditional_edges(
        "run_code_review",
        after_code_review,
        {
            "coding_review_decision": "coding_review_decision",
            "summarize": "summarize",
        },
    )
    builder.add_conditional_edges(
        "create_commit",
        after_integration_step,
        ["prepare_merge", "summarize"],
    )
    builder.add_conditional_edges(
        "prepare_merge",
        after_merge_preview,
        ["merge_approval", "summarize"],
    )
    builder.add_edge("apply_merge", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(name="AssistantCodingGraph", checkpointer=checkpointer)


def begin_coding_cycle_node(state: CodingState) -> dict[str, object]:
    """Start a plain-input cycle and atomically discard prior local state."""

    return {
        "coding_cycle_generation": int(state.get("coding_cycle_generation") or 0) + 1,
        "workspace_ref": None,
        "base_commit": None,
        "analysis_snapshot": None,
        "analysis_tasks": (),
        "analysis_results": Overwrite([]),
        "analysis_status": None,
        "analysis_snapshot_release_status": None,
        "analysis_context_consumed": False,
        "draft_artifact": None,
        "proposal": None,
        "validation": None,
        "approval_status": None,
        "approval_origin": None,
        "applied_result": None,
        "approved_changed_paths": Overwrite([]),
        "dependency_plan": None,
        "dependency_approval_status": None,
        "credential_request": None,
        "credential_approval_status": None,
        "artifact_ingress_plan": None,
        "artifact_approval_status": None,
        "format_round": 0,
        "verification_evidence": Overwrite([]),
        "validation_snapshot": None,
        "validation_binding_digest": None,
        "last_verification_status": None,
        "review_required": False,
        "review_generation": None,
        "review_snapshot": None,
        "review_snapshot_schema_version": None,
        "review_snapshot_release_status": None,
        "review_input": None,
        "review_tasks": (),
        "review_results": Overwrite([]),
        "review_report": None,
        "review_status": None,
        "review_validation_digest": None,
        "review_decision_context": None,
        "review_decision": None,
        "integration_required": False,
        "commit_result": None,
        "merge_preview": None,
        "merge_result": None,
        "repair_round": 0,
        "repair_status": None,
        "repair_failure_evidence": None,
        "repair_history": Overwrite([]),
        "repair_model_calls": 0,
        "repair_proposal_digests": Overwrite([]),
        "repair_approval_context": None,
        "coding_result": None,
    }


def _resolve_workspace(state, runtime, config, service):
    identity = authenticated_user_identity(runtime)
    thread_id = _thread_id(config)
    repo_id = str(state.get("coding_repo_id", "")).strip()
    if not thread_id or not repo_id:
        raise CodingWorkspaceError("workspace_not_allowed")
    workspace_ref = state.get("workspace_ref")
    base_commit = state.get("base_commit")
    binding_required = _has_checkpointed_cycle_state(state)
    if workspace_ref is None and base_commit is None:
        if binding_required:
            raise CodingWorkspaceError("workspace_identity_mismatch")
        return service.resolve(identity, thread_id, repo_id)
    if not workspace_ref or not base_commit:
        raise CodingWorkspaceError("workspace_identity_mismatch")
    workspace = service.get(
        str(workspace_ref),
        identity=identity,
        thread_id=thread_id,
    )
    if (
        workspace.workspace_ref != workspace_ref
        or workspace.base_commit != base_commit
        or workspace.repo_id != repo_id
    ):
        raise CodingWorkspaceError("workspace_identity_mismatch")
    _validate_analysis_checkpoint(
        state,
        identity=identity,
        thread_id=thread_id,
        workspace=workspace,
        service=service,
    )
    _validate_review_checkpoint(
        state,
        identity=identity,
        thread_id=thread_id,
        workspace=workspace,
        service=service,
    )
    return workspace


def _has_checkpointed_cycle_state(state: CodingState) -> bool:
    if (
        state.get("analysis_tasks")
        or state.get("analysis_results")
        or state.get("analysis_snapshot_release_status") is not None
        or state.get("analysis_context_consumed", False)
    ):
        return True
    return any(
        state.get(field) is not None
        for field in (
            "analysis_snapshot",
            "analysis_status",
            "draft_artifact",
            "proposal",
            "validation",
            "approval_status",
            "applied_result",
            "dependency_plan",
            "credential_request",
            "artifact_ingress_plan",
            "repair_status",
            "review_snapshot",
            "review_report",
            "review_decision",
            "coding_result",
        )
    )


def _validate_analysis_checkpoint(
    state: CodingState,
    *,
    identity: str,
    thread_id: str,
    workspace,
    service: CodingWorkspaceService,
) -> None:
    status = state.get("analysis_status")
    snapshot_value = state.get("analysis_snapshot")
    if status is None and snapshot_value is None:
        if (
            state.get("analysis_tasks")
            or state.get("analysis_results")
            or state.get("analysis_snapshot_release_status") is not None
            or state.get("analysis_context_consumed", False)
        ):
            raise ValueError("coding_analysis_contract_invalid")
        return
    if status not in {"pending", "completed", "partial", "unavailable"}:
        raise ValueError("coding_analysis_contract_invalid")
    snapshot = CodingAnalysisSnapshot.model_validate(snapshot_value)
    if (
        snapshot.workspace_ref != state.get("workspace_ref")
        or snapshot.base_commit != state.get("base_commit")
    ):
        raise CodingWorkspaceError("coding_analysis_snapshot_mismatch")
    tasks = tuple(
        CodingAnalysisTask.model_validate(task)
        for task in state.get("analysis_tasks", ())
    )
    if tasks != build_analysis_tasks():
        raise ValueError("coding_analysis_contract_invalid")
    results = tuple(
        CodingAnalysisResult.model_validate(result)
        for result in state.get("analysis_results", ())
    )
    task_ids = tuple(result.task_id for result in results)
    if len(task_ids) != len(set(task_ids)) or any(
        task_id not in ANALYSIS_TASK_IDS for task_id in task_ids
    ):
        raise ValueError("coding_analysis_contract_invalid")
    if status != "pending" and set(task_ids) != set(ANALYSIS_TASK_IDS):
        raise ValueError("coding_analysis_contract_invalid")
    joined_status, _ = join_analysis_results(snapshot, results)
    if status != "pending" and joined_status != status:
        raise ValueError("coding_analysis_contract_invalid")
    release_status = state.get("analysis_snapshot_release_status")
    if status == "pending":
        if release_status != "active":
            raise ValueError("coding_analysis_contract_invalid")
    elif release_status not in {"released", "cleanup_pending"}:
        raise ValueError("coding_analysis_contract_invalid")
    if status == "pending":
        service.validate_analysis_snapshot(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
            require_active=True,
        )


def _thread_id(config: RunnableConfig) -> str:
    return str(config.get("configurable", {}).get("thread_id", "")).strip()


def _reset_review_state() -> dict[str, object]:
    return {
        "review_required": False,
        "review_generation": None,
        "review_snapshot": None,
        "review_snapshot_schema_version": None,
        "review_snapshot_release_status": None,
        "review_input": None,
        "review_tasks": (),
        "review_results": Overwrite([]),
        "review_report": None,
        "review_status": None,
        "review_validation_digest": None,
        "review_decision_context": None,
        "review_decision": None,
    }


MAX_REVIEW_HITL_FINDINGS = 12


def _validation_evidence_digest(state: CodingState) -> str:
    binding_digest = state.get("validation_binding_digest")
    if isinstance(binding_digest, str):
        return binding_digest
    evidence = [
        item.model_dump(mode="json")
        if isinstance(item, BaseModel)
        else item
        for item in state.get("verification_evidence", ())
    ]
    return _canonical_digest(evidence)


def _review_findings_summary(findings: object) -> tuple[dict[str, object], ...]:
    if not isinstance(findings, (tuple, list)):
        return ()
    summary: list[dict[str, object]] = []
    for finding in findings[:MAX_REVIEW_HITL_FINDINGS]:
        evidence = tuple(getattr(finding, "evidence", ()))
        first = evidence[0] if evidence else None
        title = getattr(finding, "title", None) or getattr(
            finding, "summary", "review finding"
        )
        summary.append(
            {
                "finding_id": str(getattr(finding, "finding_id", ""))[:64],
                "severity": str(getattr(finding, "severity", ""))[:16],
                "category": str(getattr(finding, "category", ""))[:80],
                "title": str(title)[:240],
                "path": str(getattr(first, "path", ""))[:240],
                "line": int(getattr(first, "line", 0)),
            }
        )
    return tuple(summary)


def _review_binding_context(
    state: CodingState,
    *,
    report: CodingReviewReport | None = None,
) -> dict[str, object]:
    snapshot = CodingAnalysisSnapshot.model_validate(state.get("review_snapshot"))
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    canonical_report = report or CodingReviewReport.model_validate(
        state.get("review_report")
    )
    validation_digest = state.get("review_validation_digest")
    if not isinstance(validation_digest, str):
        raise ValueError("coding_review_binding_mismatch")
    review_input_json = review_input.model_dump(mode="json")
    expected_schema_version = _expected_review_snapshot_schema_version(
        state,
        completed=True,
    )
    if (
        snapshot.materialization_schema_version != expected_schema_version
        or review_input.snapshot_materialization_schema_version
        != expected_schema_version
        or canonical_report.snapshot_materialization_schema_version
        != expected_schema_version
        or (
            review_input.validation_evidence_digest is not None
            and (
                review_input.generation != int(state.get("review_generation"))
                or review_input.snapshot_ref != snapshot.snapshot_ref
                or review_input.tree_digest != snapshot.tree_digest
                or review_input.validation_evidence_digest != validation_digest
                or canonical_report.generation != review_input.generation
                or canonical_report.snapshot_ref != review_input.snapshot_ref
                or canonical_report.tree_digest != review_input.tree_digest
                or canonical_report.validation_evidence_digest != validation_digest
                or canonical_report.review_tasks != review_input.review_tasks
            )
        )
    ):
        raise ValueError("coding_review_binding_mismatch")
    return {
        "review_generation": int(state.get("review_generation")),
        "workspace_ref": review_input.workspace_ref,
        "base_commit": review_input.base_commit,
        "snapshot_ref": snapshot.snapshot_ref,
        "tree_digest": snapshot.tree_digest,
        "workspace_diff_digest": snapshot.workspace_diff_digest,
        "snapshot_materialization_schema_version": expected_schema_version,
        "snapshot_created_at": review_input_json["snapshot_created_at"],
        "snapshot_expires_at": review_input_json["snapshot_expires_at"],
        "patch_digest": review_input.patch_digest,
        "validation_digest": validation_digest,
        "report_digest": canonical_report.report_digest,
    }


def _review_snapshot_content_matches(
    expected: CodingAnalysisSnapshot,
    current: CodingAnalysisSnapshot,
    *,
    expected_schema_version: Literal["legacy_v1", "immutable_manifest_v2"],
) -> bool:
    """Compare immutable snapshot identity without binding to resource TTL."""

    content_matches = all(
        getattr(expected, field) == getattr(current, field)
        for field in (
            "workspace_ref",
            "base_commit",
            "tree_digest",
            "workspace_diff_digest",
        )
    )
    if (
        not content_matches
        or expected.materialization_schema_version != expected_schema_version
    ):
        return False
    if (
        expected_schema_version == current.materialization_schema_version
        and expected.snapshot_ref == current.snapshot_ref
    ):
        return True
    return (
        expected_schema_version == "legacy_v1"
        and current.materialization_schema_version == "immutable_manifest_v2"
    )


def _expected_review_snapshot_schema_version(
    state: CodingState,
    *,
    completed: bool,
) -> Literal["legacy_v1", "immutable_manifest_v2"]:
    value = state.get("review_snapshot_schema_version")
    if value in {"legacy_v1", "immutable_manifest_v2"}:
        return value
    if value is None and completed:
        return "legacy_v1"
    raise ValueError("coding_review_binding_mismatch")


def _review_decision_context_matches(
    actual: object,
    expected: Mapping[str, object],
    *,
    allow_legacy_schema_omission: bool,
) -> bool:
    if actual == expected:
        return True
    if not allow_legacy_schema_omission or not isinstance(actual, Mapping):
        return False
    legacy_expected = dict(expected)
    legacy_expected.pop("snapshot_materialization_schema_version")
    return dict(actual) == legacy_expected


def _validate_review_decision(
    raw: object,
    decision_context: Mapping[str, object],
    *,
    allow_legacy_schema_omission: bool = False,
) -> Literal["approve", "reject"]:
    if not isinstance(raw, Mapping):
        raise ValueError("coding_review_binding_mismatch")
    if raw.get("decision") not in {"approve", "reject"}:
        raise ValueError("coding_review_binding_mismatch")
    raw_context = {key: value for key, value in raw.items() if key != "decision"}
    if not _review_decision_context_matches(
        raw_context,
        decision_context,
        allow_legacy_schema_omission=allow_legacy_schema_omission,
    ):
        raise ValueError("coding_review_binding_mismatch")
    return raw["decision"]


def _validate_review_checkpoint(
    state: CodingState,
    *,
    identity: str,
    thread_id: str,
    workspace,
    service: CodingWorkspaceService,
) -> None:
    if not state.get("review_required"):
        if any(
            state.get(field) is not None
            for field in (
                "review_generation",
                "review_snapshot",
                "review_snapshot_schema_version",
                "review_input",
                "review_report",
                "review_validation_digest",
                "review_decision_context",
                "review_decision",
            )
        ):
            raise ValueError("coding_review_binding_mismatch")
        return
    if state.get("review_snapshot") is None:
        if (
            state.get("last_verification_status") == "passed"
            and all(
                state.get(field) is None
                for field in (
                    "review_generation",
                    "review_snapshot_schema_version",
                    "review_input",
                    "review_report",
                    "review_validation_digest",
                    "review_decision_context",
                    "review_decision",
                )
            )
            and not state.get("review_tasks")
            and not state.get("review_results")
        ):
            return
        raise ValueError("coding_review_binding_mismatch")
    generation = state.get("review_generation")
    snapshot = CodingAnalysisSnapshot.model_validate(state.get("review_snapshot"))
    review_input = CodingReviewInput.model_validate(state.get("review_input"))
    validation_digest = state.get("review_validation_digest")
    release_status = state.get("review_snapshot_release_status")
    applied = state.get("applied_result")
    report_value = state.get("review_report")
    expected_schema_version = _expected_review_snapshot_schema_version(
        state,
        completed=report_value is not None,
    )
    if (
        generation != state.get("coding_cycle_generation")
        or release_status not in {"active", "released"}
        or applied is None
        or state.get("last_verification_status") != "passed"
        or not isinstance(validation_digest, str)
        or validation_digest != _validation_evidence_digest(state)
        or review_input.workspace_ref != workspace.workspace_ref
        or review_input.base_commit != workspace.base_commit
        or review_input.patch_digest != applied.patch_digest
        or review_input.workspace_diff_digest != snapshot.workspace_diff_digest
        or snapshot.materialization_schema_version != expected_schema_version
        or review_input.snapshot_materialization_schema_version
        != expected_schema_version
        or review_input.snapshot_created_at != snapshot.created_at
        or review_input.snapshot_expires_at != snapshot.expires_at
        or snapshot.workspace_ref != workspace.workspace_ref
        or snapshot.base_commit != workspace.base_commit
        or (
            review_input.validation_evidence_digest is not None
            and (
                review_input.validation_evidence_digest != validation_digest
                or review_input.generation != generation
                or review_input.snapshot_ref != snapshot.snapshot_ref
                or review_input.tree_digest != snapshot.tree_digest
                or review_input.review_tasks
                != tuple(task.task_id for task in build_review_tasks())
            )
        )
    ):
        raise ValueError("coding_review_binding_mismatch")
    if report_value is None:
        if (
            release_status != "active"
            or expected_schema_version != "immutable_manifest_v2"
            or state.get("review_decision_context") is not None
            or state.get("review_decision") is not None
        ):
            raise ValueError("coding_review_binding_mismatch")
        service.validate_analysis_snapshot(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
            require_active=True,
        )
        return
    report = CodingReviewReport.model_validate(report_value)
    canonical = canonicalize_review_report(review_input, report.results)
    tasks = tuple(
        CodingReviewTask.model_validate(item)
        for item in state.get("review_tasks", ())
    )
    results = tuple(
        CodingReviewerResult.model_validate(item)
        for item in state.get("review_results", ())
    )
    if (
        report != canonical
        or report.snapshot_materialization_schema_version
        != expected_schema_version
        or (
            tasks != build_review_tasks()
            and not (
                review_input.validation_evidence_digest is None
                and tasks == build_legacy_review_tasks()
            )
        )
        or results != report.results
        or state.get("review_status") != report.status
        or not _review_decision_context_matches(
            state.get("review_decision_context"),
            _review_binding_context(state),
            allow_legacy_schema_omission=(
                expected_schema_version == "legacy_v1"
            ),
        )
    ):
        raise ValueError("coding_review_binding_mismatch")
    try:
        service.validate_analysis_snapshot(
            snapshot,
            identity=identity,
            thread_id=thread_id,
            workspace=workspace,
            require_active=False,
        )
    except CodingWorkspaceError as exc:
        if expected_schema_version != "legacy_v1" or exc.code not in {
            "coding_analysis_snapshot_missing",
            "coding_analysis_snapshot_expired",
            "coding_analysis_snapshot_legacy_manifest_missing",
        }:
            raise


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def route_analysis_workers(state: CodingState) -> list[Send] | str:
    """Fan out the fixed analysis tasks against one immutable snapshot."""

    snapshot = state.get("analysis_snapshot")
    tasks = tuple(state.get("analysis_tasks", ()))
    if snapshot is None or not tasks:
        return "join_analysis"
    completed_task_ids = {
        CodingAnalysisResult.model_validate(result).task_id
        for result in state.get("analysis_results", ())
    }
    pending_tasks = tuple(
        task for task in tasks if task.task_id not in completed_task_ids
    )
    if not pending_tasks:
        return "join_analysis"
    return [
        Send(
            "analyze_workspace",
            {
                "messages": _analysis_worker_messages(state, task, snapshot),
                "coding_repo_id": state["coding_repo_id"],
                "workspace_ref": state["workspace_ref"],
                "base_commit": state["base_commit"],
                "analysis_snapshot": snapshot,
                "analysis_task": task,
                "provider_search_profile": "none",
            },
        )
        for task in pending_tasks
        if task.task_id in ANALYSIS_TASK_IDS
    ]


def _analysis_worker_messages(
    state: CodingState,
    task: CodingAnalysisTask,
    snapshot: CodingAnalysisSnapshot,
) -> list[HumanMessage]:
    request = ""
    for message in reversed(state.get("messages", ())):
        if isinstance(message, HumanMessage):
            request = _bounded_analysis_user_text(message.content)
            break
    task_context = json.dumps(
        {
            "task_id": task.task_id,
            "objective": task.objective,
            "allowed_tool_names": list(task.allowed_tool_names),
            "snapshot_ref": snapshot.snapshot_ref,
            "tree_digest": snapshot.tree_digest,
            "instruction": (
                "Analyze only this immutable snapshot and return bounded findings. "
                "Do not propose or apply a patch."
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    messages = [HumanMessage(content=request)] if request else []
    messages.append(HumanMessage(content=task_context))
    return messages


def _bounded_analysis_user_text(content: object) -> str:
    parts: list[str] = []
    remaining_chars = _MAX_ANALYSIS_REQUEST_CHARS
    remaining_bytes = _MAX_ANALYSIS_REQUEST_BYTES

    def append_text(value: str) -> None:
        nonlocal remaining_chars, remaining_bytes
        if remaining_chars <= 0 or remaining_bytes <= 0:
            return
        candidate = value[:remaining_chars]
        encoded = candidate.encode("utf-8")
        if len(encoded) > remaining_bytes:
            candidate = encoded[:remaining_bytes].decode("utf-8", errors="ignore")
            encoded = candidate.encode("utf-8")
        parts.append(candidate)
        remaining_chars -= len(candidate)
        remaining_bytes -= len(encoded)

    if isinstance(content, str):
        append_text(content)
    elif isinstance(content, (list, tuple)):
        for block in content:
            if remaining_chars <= 0 or remaining_bytes <= 0:
                break
            if not isinstance(block, Mapping) or block.get("type") != "text":
                continue
            text_value = block.get("text")
            if isinstance(text_value, str):
                append_text(text_value)
    return "".join(parts)


def _approved_changed_paths(
    state: CodingState,
    applied: object | None = None,
) -> tuple[str, ...]:
    changed_paths = tuple(state.get("approved_changed_paths", ()))
    if changed_paths:
        return changed_paths
    return tuple(getattr(applied, "changed_paths", ()))


def _repair_history(state: CodingState) -> tuple[CodingRepairAttempt, ...]:
    return tuple(
        CodingRepairAttempt.model_validate(item)
        for item in state.get("repair_history", ())
    )


def _repair_terminal_fields(state: CodingState) -> dict[str, object]:
    status = state.get("repair_status")
    return {
        "repair_status": (
            status if status in {"passed", "exhausted", "no_progress"} else None
        ),
        "repair_history": _repair_history(state),
    }


def _failed(
    state: CodingState,
    code: str,
    *,
    repair_status: Literal["passed", "exhausted", "no_progress"] | None = None,
) -> CodingTerminalResult:
    validation = state.get("validation")
    proposal = validation.proposal if validation is not None else None
    preview = state.get("merge_preview")
    committed = state.get("commit_result")
    return CodingTerminalResult(
        status="failed",
        workspace_ref=state.get("workspace_ref"),
        base_commit=state.get("base_commit"),
        patch_digest=(proposal.patch_digest if proposal is not None else None),
        changed_paths=(
            _approved_changed_paths(state)
            or (proposal.changed_paths if proposal is not None else ())
        ),
        error_code=code,
        verification_status=(
            state.get("last_verification_status")
        ),
        verification_evidence=tuple(state.get("verification_evidence", ())),
        repair_status=(
            repair_status
            if repair_status is not None
            else _repair_terminal_fields(state)["repair_status"]
        ),
        repair_history=_repair_history(state),
        source_commit=(
            preview.source_commit
            if preview is not None
            else committed.source_commit if committed is not None else None
        ),
        expected_target_head=(
            preview.expected_target_head if preview is not None else None
        ),
        result_commit=(preview.result_commit if preview is not None else None),
        merge_preview_digest=(
            preview.merge_preview_digest if preview is not None else None
        ),
    )


__all__ = ["build_coding_graph", "route_analysis_workers"]
