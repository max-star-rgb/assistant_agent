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
    CodingMergeApprovalDecision,
    CodingPatchValidation,
    CodingRepairAttempt,
    CodingRepairFailureEvidence,
    CodingTerminalResult,
)
from assistant_agent.coding.repair import (
    ensure_repair_progress,
    repair_interrupt_payload,
    render_repair_context,
    select_repairable_failure,
    validate_repair_approval,
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
    validation_service: CodingValidationService | None = None,
    integration_service: CodingIntegrationService | None = None,
    *,
    model_call_limit: int = 8,
    tool_call_limit: int = 8,
    inspect_agent: Any | None = None,
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
        call_state = dict(state)
        call_messages = list(state.get("messages", ()))
        if state.get("repair_status") == "active":
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
        return {"messages": new_messages, "draft_artifact": artifact}

    def validate_proposal_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> dict[str, object]:
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
        update: dict[str, object] = {
            "proposal": validation.proposal,
            "validation": validation,
            "approval_status": "pending",
        }
        if state.get("repair_status") != "active":
            update.update(approval_origin="model", format_round=0)
            return update
        try:
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
                "coding_result": _failed(state, "coding_repair_no_progress"),
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
        )
        return update

    def prepare_repair_node(state: CodingState) -> dict[str, object]:
        evidence = state.get("repair_failure_evidence")
        try:
            normalized_evidence = CodingRepairFailureEvidence.model_validate(evidence)
        except Exception:
            return {
                "repair_status": "exhausted",
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
            "format_round": 0,
            "integration_required": False,
            "commit_result": None,
            "merge_preview": None,
            "merge_result": None,
            "repair_approval_context": None,
            "coding_result": None,
        }

    def approval_node(
        state: CodingState,
        runtime: Runtime[AssistantRunContext],
        config: RunnableConfig,
    ) -> Command[Literal["inspect_and_draft", "apply_patch", "summarize"]]:
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
            raw = interrupt(repair_interrupt_payload(context))
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
                    goto="inspect_and_draft",
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
            return Command(
                update={
                    "messages": [HumanMessage(content=decision.response.strip())],
                    "draft_artifact": None,
                    "proposal": None,
                    "validation": None,
                    "approval_status": None,
                    "approval_origin": "model",
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
                changed_paths=applied.changed_paths,
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
                        changed_paths=applied.changed_paths,
                        error_code="dependency_install_rejected",
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
                changed_paths=applied.changed_paths,
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
                        changed_paths=applied.changed_paths,
                        error_code="credential_lease_rejected",
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
                changed_paths=applied.changed_paths,
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
                changed_paths=applied.changed_paths,
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
                        changed_paths=applied.changed_paths,
                        error_code="artifact_ingress_rejected",
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
                changed_paths=applied.changed_paths,
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
                "format_round": int(state.get("format_round", 0))
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
        update: dict[str, object] = {"verification_evidence": list(result.evidence)}
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
        if result.status == "failed":
            current_repair_round = int(state.get("repair_round", 0))
            if state.get("repair_status") == "active":
                context = state.get("repair_approval_context")
                failure = state.get("repair_failure_evidence")
                if context is not None and failure is not None:
                    update["repair_history"] = [
                        CodingRepairAttempt(
                            round=current_repair_round,
                            failure_output_digest=failure.output_digest,
                            patch_digest=context.patch_digest,
                            workspace_diff_digest=context.workspace_diff_digest,
                            candidate_diff_digest=context.candidate_diff_digest,
                            status="failed",
                        )
                    ]
            repairable = select_repairable_failure(result, current_repair_round)
            if repairable is not None:
                update.update(
                    repair_failure_evidence=repairable,
                    repair_status="pending",
                )
                return update
            if state.get("repair_status") == "active":
                update["repair_status"] = "exhausted"
            update["coding_result"] = CodingTerminalResult(
                status="failed",
                workspace_ref=state.get("workspace_ref"),
                base_commit=state.get("base_commit"),
                patch_digest=applied.patch_digest,
                changed_paths=applied.changed_paths,
                error_code=result.error_code or "verification_command_failed",
                verification_status="failed",
                verification_evidence=all_evidence,
            )
            return update
        if state.get("repair_status") == "active":
            context = state.get("repair_approval_context")
            failure = state.get("repair_failure_evidence")
            if context is not None and failure is not None:
                update["repair_history"] = [
                    CodingRepairAttempt(
                        round=int(state.get("repair_round", 0)),
                        failure_output_digest=failure.output_digest,
                        patch_digest=context.patch_digest,
                        workspace_diff_digest=context.workspace_diff_digest,
                        candidate_diff_digest=context.candidate_diff_digest,
                        status="passed",
                    )
                ]
            update["repair_status"] = "passed"
        if repository.integration_enabled:
            update["integration_required"] = True
            return update
        update["coding_result"] = CodingTerminalResult(
            status="applied",
            workspace_ref=applied.workspace_ref,
            base_commit=applied.base_commit,
            patch_digest=applied.patch_digest,
            changed_paths=applied.changed_paths,
            verification_status="passed",
            verification_evidence=all_evidence,
        )
        return update

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
    ) -> Command[Literal["apply_merge", "summarize"]]:
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

    def after_run_validation(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("repair_status") == "pending":
            return "prepare_repair"
        if state.get("approval_status") == "pending":
            return "approval"
        if state.get("integration_required"):
            return "create_commit"
        return "summarize"

    def after_dependency_plan(state: CodingState) -> str:
        if state.get("coding_result") is not None:
            return "summarize"
        if state.get("dependency_plan") is not None:
            return "dependency_approval"
        return "plan_artifacts"

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
    builder.add_node("resolve_workspace", resolve_workspace_node)
    builder.add_node("inspect_and_draft", inspect_and_draft_node)
    builder.add_node("validate_proposal", validate_proposal_node)
    builder.add_node("prepare_repair", prepare_repair_node)
    builder.add_node("approval", approval_node)
    builder.add_node("apply_patch", apply_patch_node)
    builder.add_node("plan_dependencies", plan_dependencies_node)
    builder.add_node("dependency_approval", dependency_approval_node)
    builder.add_node("plan_credentials", plan_credentials_node)
    builder.add_node("credential_approval", credential_approval_node)
    builder.add_node("plan_artifacts", plan_artifacts_node)
    builder.add_node("artifact_approval", artifact_approval_node)
    builder.add_node("run_validation", run_validation_node)
    builder.add_node("create_commit", create_commit_node)
    builder.add_node("prepare_merge", prepare_merge_node)
    builder.add_node("merge_approval", merge_approval_node)
    builder.add_node("apply_merge", apply_merge_node)
    builder.add_node("summarize", summarize_node)
    builder.add_edge(START, "resolve_workspace")
    builder.add_conditional_edges("resolve_workspace", after_resolve)
    builder.add_edge("inspect_and_draft", "validate_proposal")
    builder.add_conditional_edges("validate_proposal", after_validation)
    builder.add_edge("prepare_repair", "inspect_and_draft")
    builder.add_edge("apply_patch", "plan_dependencies")
    builder.add_conditional_edges("plan_dependencies", after_dependency_plan)
    builder.add_conditional_edges("plan_credentials", after_credential_plan)
    builder.add_conditional_edges("plan_artifacts", after_artifact_plan)
    builder.add_conditional_edges("run_validation", after_run_validation)
    builder.add_conditional_edges("create_commit", after_integration_step)
    builder.add_conditional_edges("prepare_merge", after_merge_preview)
    builder.add_edge("apply_merge", "summarize")
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
    preview = state.get("merge_preview")
    committed = state.get("commit_result")
    return CodingTerminalResult(
        status="failed",
        workspace_ref=state.get("workspace_ref"),
        base_commit=state.get("base_commit"),
        patch_digest=(proposal.patch_digest if proposal is not None else None),
        changed_paths=(proposal.changed_paths if proposal is not None else ()),
        error_code=code,
        verification_status=(
            "passed" if state.get("verification_evidence") else None
        ),
        verification_evidence=tuple(state.get("verification_evidence", ())),
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


__all__ = ["build_coding_graph"]
