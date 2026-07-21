"""Run offline Agent eval cases."""

# ruff: noqa: E402

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from assistant_agent.agent.workflow import AgentWorkflow
from assistant_agent.agent.intent_router_adapter import create_intent_router_adapter
from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.memory.store import InMemoryStore
from assistant_agent.memory.retrieval_eval import (
    evaluate_memory_retrieval_case,
    summarize_memory_retrieval_eval_dicts,
)
from assistant_agent.memory.quality_eval import (
    evaluate_memory_quality_case,
    summarize_memory_quality_eval_dicts,
)
from assistant_agent.schemas.api import agent_run_response_from_state
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.capabilities import canonical_intent
from assistant_agent.schemas.intent_router import IntentRouterRequest
from assistant_agent.schemas.memory import MemoryItem, MemoryQuery
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.durable_tasks import TaskCheckpoint
from assistant_agent.schemas.identity import RequestIdentity
from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.tools import ToolResult
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult, ProviderChatCapabilities
from assistant_agent.services.provider_errors import build_provider_error
from assistant_agent.services.provider_policy import RetryPolicy
from assistant_agent.services.trace_store import InMemoryTraceStore
from assistant_agent.services.durable_tasks.service import DurableTaskService
from assistant_agent.services.durable_tasks.store import InMemoryTaskStore
from assistant_agent.services.durable_tasks.worker import _binding_for_lease, _resume_request
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.api.routes_tasks import _task_response
from assistant_agent.mcp.server import OfflineMCPServer


DEFAULT_CASES_PATH = ROOT / "tests" / "evals" / "eval_cases.json"
ALL_SUITES = "all"
ROUTER_MODES = {"rule", "mock_llm", "hybrid"}


class ScriptedPlanModeChatAdapter:
    """Deterministic chat adapter for plan-mode evals.

    The eval case owns native provider outputs. This adapter replays them
    through the same content/tool_calls contract used by real chat providers.
    """

    provider = "scripted-native"
    capabilities = ProviderChatCapabilities(supports_native_tools=True)

    def __init__(self, outputs: list[ChatResult]) -> None:
        self.outputs = outputs
        self.calls = 0
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        if not self.outputs:
            output = ChatResult(
                response_text="scripted output missing",
                provider=self.provider,
                model="plan-mode-eval",
                finish_reason="stop",
                message_kind="final_answer",
            )
        else:
            output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        return output


class _EvalProbeInput(BaseModel):
    text: str


class _EvalProbeTool(ToolBase):
    name = "custom_notification"
    description = "Offline durable-task confirmation probe."
    input_schema = _EvalProbeInput
    output_schema = _EvalProbeInput

    def _run(self, input: _EvalProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"summary": input.text},
            output_ref="mock://custom-notification/1",
        )


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    return [_normalize_case(case) for case in json.loads(path.read_text(encoding="utf-8"))]


def _normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(case)
    normalized.setdefault("category", "routing")
    normalized.setdefault("suite", _infer_suite(normalized))
    return normalized


def _infer_suite(case: dict[str, Any]) -> str:
    category = case.get("category")
    case_id = case.get("id")
    if category in {"multistep", "multi_step_orchestration", "shopping_search"}:
        return "e2e"
    if case_id in {
        "shopping_search_to_render",
        "image_understanding_to_render",
        "video_understanding_to_render",
        "memory_to_render",
    }:
        return "e2e"
    return "routing"


def filter_cases_by_suite(cases: list[dict[str, Any]], suite: str | None) -> list[dict[str, Any]]:
    if suite is None or suite == ALL_SUITES:
        return cases
    return [case for case in cases if case.get("suite") == suite]


def tools_contain_expected(actual_tools: list[str], expected_tools: list[str]) -> bool:
    cursor = 0
    for tool_name in actual_tools:
        if cursor < len(expected_tools) and tool_name == expected_tools[cursor]:
            cursor += 1
    return cursor == len(expected_tools)


def request_from_case(case: dict[str, Any]) -> UserRequest:
    inputs = case.get("inputs", {})
    image_ids = case.get("image_ids")
    video_ids = case.get("video_ids")
    if image_ids is None and inputs.get("has_image"):
        image_ids = [f"{case['id']}_image"]
    if video_ids is None and inputs.get("has_video"):
        video_ids = [f"{case['id']}_video"]

    payload = {
        "user_id": case.get("user_id", "u1"),
        "session_id": case.get("session_id", "s1"),
        "text": case.get("text") or case.get("user_query"),
        "image_ids": image_ids or [],
        "video_ids": video_ids or [],
        "audio_id": case.get("audio_id"),
        "metadata": dict(case.get("metadata", {})),
        "execution_strategy": case.get("execution_strategy", "react"),
    }
    if "task_execution_mode" in case:
        payload["task_execution_mode"] = case["task_execution_mode"]
    return UserRequest(**payload)


def expected_capability(case: dict[str, Any]) -> str | None:
    expected = case.get("expected_capability") or case.get("expected_intent")
    return canonical_intent(expected) if expected else None


def evaluate_case(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    if case.get("suite") == "durable_tasks":
        return evaluate_durable_task_case(case, router_mode=router_mode)
    if case.get("suite") == "plan_mode":
        return evaluate_plan_mode_case(case, router_mode=router_mode)
    if case.get("suite") == "provider_safety":
        return evaluate_provider_safety_case(case, router_mode=router_mode)
    if case.get("suite") == "memory_quality" and case.get("memory_quality_eval"):
        return evaluate_memory_quality_case_detail(case, router_mode=router_mode)
    if case.get("suite") == "memory" and case.get("memory_scenario"):
        return evaluate_memory_case(case, router_mode=router_mode)
    if case.get("suite") == "packaging":
        return evaluate_packaging_case(case, router_mode=router_mode)

    request = request_from_case(case)
    router_expectation = _router_expectation(case, router_mode)
    if router_mode == "rule":
        state = AgentWorkflow().run(request)
        actual_intent = state.intent.intent if state.intent else None
        actual_tools = [call.tool_name for call in state.tool_calls]
        missing_slots = state.intent.missing_slots if state.intent else []
        response_text = state.response.message if state.response else ""
    else:
        decision = create_intent_router_adapter(
            ProviderConfig(intent_router=router_mode)
        ).decide(IntentRouterRequest.from_user_request(request))
        state = None
        actual_intent = decision.primary_intent
        actual_tools = [step.tool_name for step in decision.plan_steps if step.tool_name]
        missing_slots = decision.missing_inputs
        response_text = ""
    actual_capability = canonical_intent(actual_intent) if actual_intent else None
    expected_tools = router_expectation.get("expected_tools", case.get("expected_tools", []))
    expected_intent = router_expectation.get("expected_intent", case.get("expected_intent"))
    expected_capability_name = expected_capability(case)
    if router_expectation.get("expected_capability") or router_expectation.get("expected_intent"):
        expected_capability_name = canonical_intent(
            router_expectation.get("expected_capability") or router_expectation.get("expected_intent")
        )
    must_not_call = case.get("must_not_call", [])
    must_not_require = case.get("must_not_require", [])

    intent_match = (
        actual_intent == expected_intent
        if actual_intent is None or expected_intent is None
        else canonical_intent(actual_intent) == canonical_intent(expected_intent)
    )
    capability_match = actual_capability == expected_capability_name
    tool_selection_match = set(expected_tools).issubset(set(actual_tools))
    ordered_tool_match = tools_contain_expected(actual_tools, expected_tools)
    unexpected_tools = [tool for tool in actual_tools if tool in must_not_call]
    media_requirement_errors = [slot for slot in missing_slots if slot in must_not_require]
    followup_expected = expected_capability_name == "ask_followup"
    followup_match = (actual_capability == "ask_followup") if followup_expected else True
    expected_response_contains = case.get("expected_response_contains", [])
    if router_mode != "rule":
        expected_response_contains = []
    response_contains_match = all(
        expected in response_text for expected in expected_response_contains
    )
    passed = (
        intent_match
        and capability_match
        and tool_selection_match
        and ordered_tool_match
        and not unexpected_tools
        and not media_requirement_errors
        and followup_match
        and response_contains_match
    )
    return {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": passed,
        "intent_match": intent_match,
        "capability_match": capability_match,
        "tool_selection_match": tool_selection_match,
        "ordered_tool_match": ordered_tool_match,
        "unexpected_tool_called": bool(unexpected_tools),
        "media_requirement_error": bool(media_requirement_errors),
        "followup_expected": followup_expected,
        "followup_match": followup_match,
        "response_contains_match": response_contains_match,
        "expected_intent": expected_intent,
        "actual_intent": actual_intent,
        "expected_capability": expected_capability_name,
        "actual_capability": actual_capability,
        "expected_tools": expected_tools,
        "actual_tools": actual_tools,
        "expected_response_contains": expected_response_contains,
        "must_not_call": must_not_call,
        "unexpected_tools": unexpected_tools,
        "must_not_require": must_not_require,
        "missing_slots": missing_slots,
        "media_requirement_errors": media_requirement_errors,
    }


def evaluate_durable_task_case(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    """Evaluate durable-task control flow through the offline native runtime."""

    scenario = str(case.get("durable_scenario") or "")
    registry = create_default_registry()
    if scenario == "waiting_confirmation":
        registry.register(_EvalProbeTool())
    service = DurableTaskService(store=InMemoryTaskStore(), registry=registry)
    adapter = ScriptedPlanModeChatAdapter(_durable_chat_outputs(case))
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(durable_tasks_enabled=True),
        chat_adapter=adapter,
        durable_task_service=service,
    )
    state = None
    bundle = None

    if scenario in {"simple_auto_direct", "auto_complex_submission", "explicit_submission", "mixed_batch"}:
        state = runtime.run_state(request_from_case(case))
        task_data = (state.response.data or {}).get("task") if state.response else None
        task_id = task_data.get("task_id") if isinstance(task_data, dict) else None
        bundle = service.store.load(task_id) if isinstance(task_id, str) else None
    else:
        tool_name = "custom_notification" if scenario == "waiting_confirmation" else "shopping_search"
        bundle = _submit_durable_eval_task(service, tool_name=tool_name)
        if scenario == "final_completion":
            lease = service.claim_next(worker_id="eval-worker")
            stored = service.checkpoint(
                lease,
                TaskCheckpoint(
                    kind="tool_succeeded",
                    step_id="step_1",
                    summary="precompleted step",
                    output_ref="mock://precompleted/1",
                ),
            )
            service.store.release(lease, expected_version=stored.task.version)
        lease = service.claim_next(worker_id="eval-worker")
        snapshot = service.snapshot_for_lease(lease)
        leased = service.store.load(lease.task_id)
        binding = _binding_for_lease(lease, leased, snapshot.ready_step_ids)
        request = _resume_request(leased.task.user_id, leased.task.session_id, snapshot, binding)
        quantum = runtime.run_task_quantum(request, binding=binding)
        state = quantum.state
        checkpoint_lease = lease
        if quantum.binding is not None:
            checkpoint_lease = lease.model_copy(
                update={"task_version": quantum.binding.task_version}
            )
        bundle = service.checkpoint(checkpoint_lease, quantum.checkpoint)

    actual_tools = [call.tool_name for call in state.tool_calls] if state is not None else []
    error_codes = [
        run.error_code
        for run in (bundle.step_runs if bundle is not None else [])
        if run.error_code
    ]
    if state is not None:
        error_codes.extend(
            str(error.details.get("code"))
            for error in state.errors
            if error.details.get("code")
        )
    task_status = bundle.task.status if bundle is not None else None
    plan_version = bundle.task.current_plan_version if bundle is not None else None
    public_payload = (
        _task_response(bundle).model_dump_json()
        if bundle is not None
        else (state.response.model_dump_json() if state is not None and state.response else "")
    )
    raw_payload_safe = not any(
        marker in public_payload
        for marker in ("lease_token", "binding_digest", "idempotency_key", "raw_provider_payload")
    )
    expected_tools = case.get("expected_tools", [])
    expected_status = case.get("expected_task_status")
    expected_plan_version = case.get("expected_plan_version")
    expected_errors = case.get("expected_error_codes", [])
    must_not_call = case.get("must_not_call", [])
    unexpected_tools = [name for name in actual_tools if name in must_not_call]
    tool_match = tools_contain_expected(actual_tools, expected_tools)
    status_match = expected_status is None or task_status == expected_status
    plan_match = expected_plan_version is None or plan_version == expected_plan_version
    chat_calls_match = case.get("expected_chat_calls") in {None, adapter.calls}
    errors_match = all(code in error_codes for code in expected_errors)
    governance_evidence = (
        tool_match
        and not unexpected_tools
        and raw_payload_safe
        and errors_match
    )
    passed = status_match and plan_match and chat_calls_match and governance_evidence
    return {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": passed,
        "intent_match": True,
        "capability_match": True,
        "tool_selection_match": tool_match,
        "ordered_tool_match": tool_match,
        "unexpected_tool_called": bool(unexpected_tools),
        "media_requirement_error": False,
        "followup_expected": False,
        "followup_match": True,
        "response_contains_match": True,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": case.get("expected_intent"),
        "expected_capability": case.get("expected_capability"),
        "actual_capability": case.get("expected_capability"),
        "expected_tools": expected_tools,
        "actual_tools": actual_tools,
        "expected_response_contains": [],
        "must_not_call": must_not_call,
        "unexpected_tools": unexpected_tools,
        "must_not_require": [],
        "missing_slots": [],
        "media_requirement_errors": [],
        "task_status": task_status,
        "plan_version": plan_version,
        "chat_calls": adapter.calls,
        "error_codes": error_codes,
        "raw_payload_safe": raw_payload_safe,
        "durable_task_checks": {
            "status_match": status_match,
            "plan_version_match": plan_match,
            "chat_calls_match": chat_calls_match,
            "error_codes_match": errors_match,
            "tool_governance_evidence": governance_evidence,
        },
    }


def _submit_durable_eval_task(service: DurableTaskService, *, tool_name: str):
    return service.submit_plan(
        identity=RequestIdentity.for_user(user_id="u1", session_id="s1"),
        ingress_run_id="eval-ingress",
        plan=TaskPlan(
            goal="offline durable eval task",
            steps=[TaskStep(step_id="step_1", action="execute", tool_name=tool_name)],
        ),
        revision_reason="initial",
    )


def evaluate_plan_mode_case(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    """Evaluate plan-mode hints against the current native tool runtime."""

    expected_tools = case.get("expected_tools", [])
    must_not_call = case.get("must_not_call", [])
    expected_response_contains = case.get("expected_response_contains", [])
    expected_plan_mode_hint = case.get("execution_strategy", "react")
    expected_native_runtime = case.get("expected_native_runtime", True)
    expected_plan_status = case.get("expected_plan_status")
    expected_final_answer_source = case.get("expected_final_answer_source")
    expected_plan_revision_count = case.get("expected_plan_revision_count")
    expected_decision_types = case.get("expected_decision_types", [])
    expected_observation_tools = case.get("expected_observation_tools", [])
    expected_trace_nodes = case.get("expected_trace_nodes", [])
    expected_error_codes = case.get("expected_error_codes", [])
    expected_chat_calls = case.get("expected_chat_calls")

    adapter = ScriptedPlanModeChatAdapter(_plan_mode_chat_outputs(case))
    trace_store = InMemoryTraceStore()
    runtime = AgentGraphRuntime(
        config=ProviderConfig(agent_graph_mode="assistant_loop"),
        chat_adapter=adapter,
        trace_store=trace_store,
    )
    state = runtime.run_state(request_from_case(case))
    api_response = agent_run_response_from_state(state)

    actual_tools = [call.tool_name for call in state.tool_calls]
    response_text = state.response.message if state.response else ""
    unexpected_tools = [tool for tool in actual_tools if tool in must_not_call]
    decision_types = [
        step.get("decision_type")
        for step in api_response.react_steps
        if step.get("decision_type")
    ]
    observation_tools = [
        step.get("observation_tool")
        for step in api_response.react_steps
        if step.get("observation_tool")
    ]
    error_codes = [
        str(error.details.get("code", "unknown_error"))
        for error in state.errors
    ]
    trace_nodes = trace_store.node_path(state.run_id)
    plan_transition_calls = sum(
        step.get("decision_type") in {"enter_plan_mode", "exit_plan_mode", "plan_rejected"}
        for step in api_response.react_steps
    )

    plan_mode_hint_match = state.execution_strategy == expected_plan_mode_hint == api_response.execution_strategy
    native_runtime_match = (
        expected_native_runtime is None
        or bool(state.request.metadata.get("native_runtime")) is bool(expected_native_runtime)
    )
    plan_status_match = expected_plan_status is None or state.plan_status == expected_plan_status
    final_answer_source_match = (
        expected_final_answer_source is None
        or api_response.data.get("final_answer_source") == expected_final_answer_source
    )
    plan_revision_match = (
        expected_plan_revision_count is None
        or state.plan_revision_count == expected_plan_revision_count
    )
    decision_types_match = all(item in decision_types for item in expected_decision_types)
    observation_tools_match = tools_contain_expected(
        [str(item) for item in observation_tools],
        expected_observation_tools,
    )
    trace_nodes_match = all(node in trace_nodes for node in expected_trace_nodes)
    error_codes_match = all(code in error_codes for code in expected_error_codes)
    chat_calls_match = expected_chat_calls is None or adapter.calls == expected_chat_calls
    api_contract_match = (
        api_response.run_id == state.run_id
        and api_response.trace_id == state.trace_id
        and api_response.execution_strategy == state.execution_strategy
        and isinstance(api_response.react_steps, list)
        and isinstance(api_response.decision_trace, list)
    )
    tool_selection_match = set(expected_tools).issubset(set(actual_tools))
    ordered_tool_match = tools_contain_expected(actual_tools, expected_tools)
    response_contains_match = all(expected in response_text for expected in expected_response_contains)

    passed = (
        plan_mode_hint_match
        and native_runtime_match
        and plan_status_match
        and final_answer_source_match
        and plan_revision_match
        and decision_types_match
        and observation_tools_match
        and trace_nodes_match
        and error_codes_match
        and chat_calls_match
        and api_contract_match
        and tool_selection_match
        and ordered_tool_match
        and not unexpected_tools
        and response_contains_match
    )
    expected_capability_name = expected_capability(case)
    actual_capability = expected_capability_name if plan_mode_hint_match else None
    return {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": passed,
        "intent_match": plan_mode_hint_match,
        "capability_match": plan_mode_hint_match,
        "tool_selection_match": tool_selection_match,
        "ordered_tool_match": ordered_tool_match,
        "unexpected_tool_called": bool(unexpected_tools),
        "media_requirement_error": False,
        "followup_expected": False,
        "followup_match": True,
        "response_contains_match": response_contains_match,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": state.execution_strategy,
        "expected_capability": expected_capability_name,
        "actual_capability": actual_capability,
        "expected_tools": expected_tools,
        "actual_tools": actual_tools,
        "expected_response_contains": expected_response_contains,
        "must_not_call": must_not_call,
        "unexpected_tools": unexpected_tools,
        "must_not_require": case.get("must_not_require", []),
        "missing_slots": [],
        "media_requirement_errors": [],
        "plan_mode_checks": {
            "plan_mode_hint_match": plan_mode_hint_match,
            "native_runtime_match": native_runtime_match,
            "plan_status_match": plan_status_match,
            "final_answer_source_match": final_answer_source_match,
            "plan_revision_match": plan_revision_match,
            "decision_types_match": decision_types_match,
            "observation_tools_match": observation_tools_match,
            "trace_nodes_match": trace_nodes_match,
            "error_codes_match": error_codes_match,
            "chat_calls_match": chat_calls_match,
            "api_contract_match": api_contract_match,
        },
        "execution_strategy": state.execution_strategy,
        "plan_status": state.plan_status,
        "plan_revision_count": state.plan_revision_count,
        "decision_types": decision_types,
        "observation_tools": observation_tools,
        "error_codes": error_codes,
        "trace_nodes": trace_nodes,
        "chat_calls": adapter.calls,
        "plan_transition_calls": plan_transition_calls,
    }


def evaluate_packaging_case(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    """Evaluate offline MCP packaging checks."""

    scenario = case.get("packaging_scenario")
    if scenario == "mcp_tool_inventory":
        tools = {tool["name"] for tool in OfflineMCPServer().list_tools()}
        passed = {"agent_run", "tool_list", "tool_run", "demo_flow_run"}.issubset(tools)
    elif scenario == "mcp_smoke_redaction":
        result = OfflineMCPServer().call_tool("missing_tool", {"Authorization": "Bearer secret-token"})
        payload = result.model_dump_json()
        passed = result.status == "failed" and "secret-token" not in payload and "Authorization" not in payload
    else:
        passed = False
    return {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": passed,
        "intent_match": True,
        "capability_match": True,
        "tool_selection_match": True,
        "ordered_tool_match": True,
        "unexpected_tool_called": False,
        "media_requirement_error": False,
        "followup_expected": False,
        "followup_match": True,
        "response_contains_match": True,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": case.get("expected_intent"),
        "expected_capability": case.get("expected_capability"),
        "actual_capability": case.get("expected_capability"),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": case.get("expected_tools", []),
        "expected_response_contains": case.get("expected_response_contains", []),
        "must_not_call": case.get("must_not_call", []),
        "unexpected_tools": [],
        "must_not_require": case.get("must_not_require", []),
        "missing_slots": [],
        "media_requirement_errors": [],
    }


def evaluate_memory_case(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    """Evaluate offline memory store scenarios without external services."""

    scenario = case.get("memory_scenario")
    store = InMemoryStore()
    now = case.get("created_at", "2026-01-01T00:00:00+00:00")
    item = MemoryItem(
        memory_id="m_shared",
        user_id="user_a",
        session_id="s1",
        memory_type="preference",
        summary="用户 A 喜欢日系极简浅色背景",
        created_at=now,
    )
    store.save(item)
    if scenario == "user_isolation":
        result = store.search(MemoryQuery(user_id="user_b", query="日系极简", top_k=5))
        passed = result.items == [] and "用户 A" not in result.memory_context
    elif scenario == "delete_user_scoped":
        store.save(item.model_copy(update={"user_id": "user_b"}))
        passed = store.delete("user_a", "m_shared") and store.get("user_b", "m_shared") is not None
    elif scenario == "retrieval_eval":
        eval_result = evaluate_memory_retrieval_case(
            {
                "id": case["id"],
                **case.get("memory_retrieval_eval", {}),
            }
        )
        passed = eval_result.passed
    else:
        passed = False
    detail = {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": passed,
        "intent_match": True,
        "capability_match": True,
        "tool_selection_match": True,
        "ordered_tool_match": True,
        "unexpected_tool_called": False,
        "media_requirement_error": False,
        "followup_expected": False,
        "followup_match": True,
        "response_contains_match": True,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": case.get("expected_intent"),
        "expected_capability": case.get("expected_capability"),
        "actual_capability": case.get("expected_capability"),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": case.get("expected_tools", []),
        "expected_response_contains": case.get("expected_response_contains", []),
        "must_not_call": case.get("must_not_call", []),
        "unexpected_tools": [],
        "must_not_require": case.get("must_not_require", []),
        "missing_slots": [],
        "media_requirement_errors": [],
    }
    if scenario == "retrieval_eval":
        detail["memory_retrieval_eval"] = eval_result.model_dump(mode="json")
    return detail


def evaluate_memory_quality_case_detail(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    """Evaluate offline memory write-quality cases without external services."""

    eval_result = evaluate_memory_quality_case(
        {
            "id": case["id"],
            **case.get("memory_quality_eval", {}),
        }
    )
    return {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": eval_result.passed,
        "intent_match": True,
        "capability_match": True,
        "tool_selection_match": True,
        "ordered_tool_match": True,
        "unexpected_tool_called": False,
        "media_requirement_error": False,
        "followup_expected": False,
        "followup_match": True,
        "response_contains_match": True,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": case.get("expected_intent"),
        "expected_capability": case.get("expected_capability"),
        "actual_capability": case.get("expected_capability"),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": case.get("expected_tools", []),
        "expected_response_contains": case.get("expected_response_contains", []),
        "must_not_call": case.get("must_not_call", []),
        "unexpected_tools": [],
        "must_not_require": case.get("must_not_require", []),
        "missing_slots": [],
        "media_requirement_errors": [],
        "memory_quality_eval": eval_result.model_dump(mode="json"),
    }


def evaluate_provider_safety_case(case: dict[str, Any], router_mode: str = "rule") -> dict[str, Any]:
    """Evaluate offline provider safety cases without calling providers."""

    scenario = case.get("safety_scenario")
    expected_code = case.get("expected_error_code")
    result = _provider_safety_result(scenario)
    actual_code = result["code"]
    sanitized_text = result.get("message", "")
    passed = (
        actual_code == expected_code
        and "sk-" not in sanitized_text
        and "Bearer" not in sanitized_text
        and "Authorization" not in sanitized_text
        and "raw provider response" not in sanitized_text
    )
    return {
        "id": case["id"],
        "router_mode": router_mode,
        "suite": case.get("suite"),
        "category": case.get("category"),
        "scenario_id": case.get("scenario_id"),
        "passed": passed,
        "intent_match": True,
        "capability_match": True,
        "tool_selection_match": True,
        "ordered_tool_match": True,
        "unexpected_tool_called": False,
        "media_requirement_error": False,
        "followup_expected": False,
        "followup_match": True,
        "response_contains_match": True,
        "expected_intent": case.get("expected_intent"),
        "actual_intent": case.get("expected_intent"),
        "expected_capability": case.get("expected_capability"),
        "actual_capability": case.get("expected_capability"),
        "expected_tools": case.get("expected_tools", []),
        "actual_tools": case.get("expected_tools", []),
        "expected_response_contains": case.get("expected_response_contains", []),
        "must_not_call": case.get("must_not_call", []),
        "unexpected_tools": [],
        "must_not_require": case.get("must_not_require", []),
        "missing_slots": [],
        "media_requirement_errors": [],
        "expected_error_code": expected_code,
        "actual_error_code": actual_code,
    }


def _provider_safety_result(scenario: str | None) -> dict[str, Any]:
    if scenario == "provider_timeout":
        policy = RetryPolicy(max_retries=1)
        error = build_provider_error("provider_timeout", "provider timed out after 10s")
        return {"code": error.code, "message": error.message, "retryable": policy.is_retryable(error.code)}
    if scenario == "provider_bad_response":
        error = build_provider_error(
            "provider_bad_response",
            "Authorization: Bearer sk-test provider payload was invalid",
            detail={"raw_response": "raw provider response body"},
        )
        return {"code": error.code, "message": error.message, "retryable": RetryPolicy().is_retryable(error.code)}
    if scenario == "provider_unconfigured":
        error = build_provider_error("provider_unconfigured", "missing OPENAI_API_KEY")
        return {"code": error.code, "message": error.message, "retryable": RetryPolicy().is_retryable(error.code)}
    if scenario == "provider_rate_limited":
        policy = RetryPolicy(max_retries=1)
        error = build_provider_error("provider_rate_limited", "provider rate limit reached")
        return {"code": error.code, "message": error.message, "retryable": policy.is_retryable(error.code)}
    return {"code": "unknown_error", "message": "unknown provider safety scenario", "retryable": False}


def _plan_mode_chat_outputs(case: dict[str, Any]) -> list[ChatResult]:
    outputs = case.get("scripted_chat_outputs", [])
    rendered: list[ChatResult] = []
    for index, output in enumerate(outputs, start=1):
        if isinstance(output, dict):
            output_type = output.get("type")
            if output_type == "tool_call":
                tool_name = str(output.get("tool_name") or "")
                rendered.append(
                    ChatResult(
                        response_text=str(output.get("preamble") or ""),
                        tool_calls=[
                            NativeToolCall(
                                id=f"eval_call_{index}",
                                name=tool_name,
                                arguments=output.get("tool_input") if isinstance(output.get("tool_input"), dict) else {},
                                raw={
                                    "id": f"eval_call_{index}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(output.get("tool_input") or {}, ensure_ascii=False),
                                    },
                                },
                            )
                        ],
                        provider="scripted-native",
                        model="plan-mode-eval",
                        finish_reason="tool_calls",
                        message_kind="tool_call",
                    )
                )
                continue
            if output_type in {"final_answer", "exit_plan_mode"}:
                rendered.append(
                    ChatResult(
                        response_text=str(output.get("message") or "已处理请求。"),
                        provider="scripted-native",
                        model="plan-mode-eval",
                        finish_reason="stop",
                        message_kind="final_answer",
                    )
                )
                continue
        rendered.append(
            ChatResult(
                response_text=str(output),
                provider="scripted-native",
                model="plan-mode-eval",
                finish_reason="stop",
                message_kind="final_answer",
            )
        )
    return rendered


def _durable_chat_outputs(case: dict[str, Any]) -> list[ChatResult]:
    outputs = case.get("scripted_chat_outputs", [])
    rendered: list[ChatResult] = []
    for index, output in enumerate(outputs, start=1):
        if isinstance(output, dict) and output.get("type") == "tool_batch":
            calls = output.get("calls") if isinstance(output.get("calls"), list) else []
            rendered.append(
                ChatResult(
                    response_text="",
                    tool_calls=[
                        NativeToolCall(
                            id=f"durable_eval_{index}_{call_index}",
                            name=str(call.get("tool_name") or ""),
                            arguments=(
                                call.get("tool_input")
                                if isinstance(call.get("tool_input"), dict)
                                else {}
                            ),
                        )
                        for call_index, call in enumerate(calls, start=1)
                        if isinstance(call, dict)
                    ],
                    provider="scripted-native",
                    model="durable-task-eval",
                    finish_reason="tool_calls",
                    message_kind="tool_call",
                )
            )
            continue
        rendered.extend(_plan_mode_chat_outputs({"scripted_chat_outputs": [output]}))
    return rendered


def _router_expectation(case: dict[str, Any], router_mode: str) -> dict[str, Any]:
    expectations = case.get("router_expectations", {})
    return expectations.get(router_mode, {})


def run_evals(cases: list[dict[str, Any]], router_mode: str = "rule") -> dict[str, Any]:
    details = [evaluate_case(case, router_mode=router_mode) for case in cases]
    summary = summarize_details(details)
    summary["router_mode"] = router_mode
    summary["routers"] = {router_mode: {key: value for key, value in summary.items() if key != "routers"}}
    return summary


def summarize_details(details: list[dict[str, Any]], include_suites: bool = True) -> dict[str, Any]:
    failed_case_ids = [detail["id"] for detail in details if not detail["passed"]]

    total = len(details)
    failed = len(failed_case_ids)
    passed_count = total - failed
    intent_matches = sum(1 for detail in details if detail["intent_match"])
    capability_matches = sum(1 for detail in details if detail["capability_match"])
    tool_selection_matches = sum(1 for detail in details if detail["tool_selection_match"])
    ordered_tool_matches = sum(1 for detail in details if detail["ordered_tool_match"])
    unexpected_tool_cases = sum(1 for detail in details if detail["unexpected_tool_called"])
    media_requirement_error_cases = sum(1 for detail in details if detail["media_requirement_error"])
    followup_cases = [detail for detail in details if detail["followup_expected"]]
    followup_matches = sum(1 for detail in followup_cases if detail["followup_match"])
    response_quality_cases = [
        detail for detail in details if detail["expected_response_contains"]
    ]
    response_quality_matches = sum(
        1 for detail in response_quality_cases if detail["response_contains_match"]
    )
    suite_summaries = {}
    if include_suites:
        suite_summaries = {
            suite: summarize_details(
                [detail for detail in details if detail.get("suite") == suite],
                include_suites=False,
            )
            for suite in sorted({detail.get("suite") for detail in details if detail.get("suite")})
        }
    memory_retrieval_results = [
        detail["memory_retrieval_eval"]
        for detail in details
        if isinstance(detail.get("memory_retrieval_eval"), dict)
    ]
    memory_quality_results = [
        detail["memory_quality_eval"]
        for detail in details
        if isinstance(detail.get("memory_quality_eval"), dict)
    ]
    summary = {
        "total": total,
        "passed": passed_count,
        "failed": failed,
        "suites": suite_summaries,
        "pass_rate": passed_count / total if total else 0.0,
        "intent_accuracy": intent_matches / total if total else 0.0,
        "capability_accuracy": capability_matches / total if total else 0.0,
        "tool_selection_accuracy": tool_selection_matches / total if total else 0.0,
        "ordered_tool_match": ordered_tool_matches / total if total else 0.0,
        "unexpected_tool_rate": unexpected_tool_cases / total if total else 0.0,
        "media_requirement_error_rate": media_requirement_error_cases / total if total else 0.0,
        "followup_accuracy": followup_matches / len(followup_cases) if followup_cases else 1.0,
        "response_quality_pass_rate": (
            response_quality_matches / len(response_quality_cases)
            if response_quality_cases
            else 1.0
        ),
        "failed_case_ids": failed_case_ids,
    }
    if memory_retrieval_results:
        summary["memory_retrieval_eval"] = summarize_memory_retrieval_eval_dicts(memory_retrieval_results)
    if memory_quality_results:
        summary["memory_quality_eval"] = summarize_memory_quality_eval_dicts(memory_quality_results)
    return summary


def main() -> int:
    parser = ArgumentParser(description="Run offline Agent eval cases.")
    parser.add_argument(
        "--suite",
        default=ALL_SUITES,
        help="Eval suite to run, for example: routing, e2e, or all.",
    )
    parser.add_argument(
        "--router",
        default="rule",
        choices=sorted(ROUTER_MODES),
        help="Intent router mode to evaluate: rule, mock_llm, or hybrid.",
    )
    args = parser.parse_args()

    cases = filter_cases_by_suite(load_cases(), args.suite)
    summary = run_evals(cases, router_mode=args.router)
    summary["selected_suite"] = args.suite
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
