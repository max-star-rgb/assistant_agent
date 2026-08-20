"""Sequential native graph for governed AI coding patch approval."""

from __future__ import annotations

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
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from assistant_agent.coding.models import (
    CodingApprovalDecision,
    CodingPatchValidation,
    CodingTerminalResult,
)
from assistant_agent.coding.workspace import CodingWorkspaceError, CodingWorkspaceService
from assistant_agent.native_agent.context import (
    AssistantRunContext,
    authenticated_user_identity,
)
from assistant_agent.native_agent.state import CodingState


_CODING_PROMPT = (
    "你是隔离 worktree 中的代码修改 Agent。先用只读 coding_repo_* 工具检查必要上下文，"
    "然后调用 coding_propose_patch 提交一份完整 unified diff。不要假设存在 shell、测试、commit、"
    "merge、push、删除或直接写文件能力；proposal 通过确定性校验后必须等待用户审批。"
)


def build_coding_graph(
    model: Any,
    tools: list[BaseTool],
    workspace_service: CodingWorkspaceService,
    *,
    model_call_limit: int = 8,
    tool_call_limit: int = 8,
    inspect_agent: Any | None = None,
    checkpointer: Any | None = None,
):
    """Build the deterministic inspect, approve, and apply sequence."""

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

    def resolve_workspace_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
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
            "workspace_ref": workspace.workspace_ref,
            "base_commit": workspace.base_commit,
        }

    async def inspect_and_draft_node(
        state: CodingState,
        config: RunnableConfig,
    ) -> dict[str, object]:
        if state.get("coding_result") is not None:
            return {}
        before = len(state.get("messages", ()))
        result = await inspect_agent.ainvoke(dict(state), config=config)
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
        return {"messages": new_messages, "draft_artifact": artifact}

    def validate_proposal_node(state: CodingState) -> dict[str, object]:
        if state.get("coding_result") is not None:
            return {}
        artifact = state.get("draft_artifact")
        if not isinstance(artifact, dict):
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
            return {
                "coding_result": CodingTerminalResult(
                    status="failed",
                    workspace_ref=state.get("workspace_ref"),
                    base_commit=state.get("base_commit"),
                    error_code="patch_invalid",
                )
            }
        return {
            "proposal": validation.proposal,
            "validation": validation,
            "approval_status": "pending",
        }

    def approval_node(
        state: CodingState,
    ) -> Command[Literal["inspect_and_draft", "apply_patch", "summarize"]]:
        validation = state.get("validation")
        if validation is None:
            return Command(goto="summarize")
        proposal = validation.proposal
        raw = interrupt(
            {
                "action": "coding_patch_apply",
                "workspace_ref": state.get("workspace_ref"),
                "base_commit": proposal.base_commit,
                "patch_digest": proposal.patch_digest,
                "changed_paths": list(proposal.changed_paths),
                "summary": proposal.summary,
                "diff_preview": validation.diff_preview,
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
            return Command(
                update={
                    "messages": [HumanMessage(content=decision.response.strip())],
                    "draft_artifact": None,
                    "proposal": None,
                    "validation": None,
                    "approval_status": None,
                },
                goto="inspect_and_draft",
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
            "coding_result": CodingTerminalResult(
                status="applied",
                workspace_ref=applied.workspace_ref,
                base_commit=applied.base_commit,
                patch_digest=applied.patch_digest,
                changed_paths=applied.changed_paths,
            ),
        }

    def summarize_node(state: CodingState) -> dict[str, object]:
        result = state.get("coding_result") or _failed(state, "patch_invalid")
        return {
            "coding_result": result,
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
        return "summarize" if state.get("coding_result") is not None else "inspect_and_draft"

    def after_validation(state: CodingState) -> str:
        return "summarize" if state.get("coding_result") is not None else "approval"

    builder = StateGraph(CodingState, context_schema=AssistantRunContext)
    builder.add_node("resolve_workspace", resolve_workspace_node)
    builder.add_node("inspect_and_draft", inspect_and_draft_node)
    builder.add_node("validate_proposal", validate_proposal_node)
    builder.add_node("approval", approval_node)
    builder.add_node("apply_patch", apply_patch_node)
    builder.add_node("summarize", summarize_node)
    builder.add_edge(START, "resolve_workspace")
    builder.add_conditional_edges("resolve_workspace", after_resolve)
    builder.add_edge("inspect_and_draft", "validate_proposal")
    builder.add_conditional_edges("validate_proposal", after_validation)
    builder.add_edge("apply_patch", "summarize")
    builder.add_edge("summarize", END)
    return builder.compile(name="AssistantCodingGraph", checkpointer=checkpointer)


def _resolve_workspace(state, runtime, config, service):
    identity = authenticated_user_identity(runtime)
    thread_id = str(config.get("configurable", {}).get("thread_id", "")).strip()
    repo_id = str(state.get("coding_repo_id", "")).strip()
    if not thread_id or not repo_id:
        raise CodingWorkspaceError("workspace_not_allowed")
    return service.resolve(identity, thread_id, repo_id)


def _failed(state: CodingState, code: str) -> CodingTerminalResult:
    validation = state.get("validation")
    proposal = validation.proposal if validation is not None else None
    return CodingTerminalResult(
        status="failed",
        workspace_ref=state.get("workspace_ref"),
        base_commit=state.get("base_commit"),
        patch_digest=(proposal.patch_digest if proposal is not None else None),
        changed_paths=(proposal.changed_paths if proposal is not None else ()),
        error_code=code,
    )


__all__ = ["build_coding_graph"]
