"""Governed read-only tool probes for proactive wake rules."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.agent.tool_executor import ToolExecutor
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.proactive_wake import WakeRule, WakeSignal
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.services.context.compaction import compact_observation_for_context
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
from assistant_agent.tools.registry import ToolRegistry


class ProactiveRuleValidation(BaseModel):
    accepted: bool
    code: str
    message: str


class ProactiveRuleValidator:
    def __init__(self, *, registry: ToolRegistry, allowed_tool_names: set[str]) -> None:
        self.registry = registry
        self.allowed_tool_names = frozenset(allowed_tool_names)

    def validate(self, rule: WakeRule) -> ProactiveRuleValidation:
        if not rule.enabled:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_rule_disabled",
                message="Disabled rules cannot run proactive probes.",
            )
        if rule.condition.mode != "changed":
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_condition_mode_unsupported",
                message="Phase 1 only supports condition.mode=changed.",
            )
        if rule.probe.tool_name not in self.allowed_tool_names:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_not_allowed",
                message="Probe tool is not in the proactive allowlist.",
            )
        spec = next(
            (item for item in self.registry.list_specs() if item.name == rule.probe.tool_name),
            None,
        )
        if spec is None:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_unknown",
                message="Probe tool is not registered.",
            )
        view = ToolPolicyInterpreter().view_for_spec(spec)
        if (
            view.side_effect_level not in {"none", "local_read", "external_read"}
            or view.requires_confirmation
            or not view.auto_executable
            or bool(view.resource_writes)
        ):
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_not_read_only",
                message=(
                    "Probe tool must be auto-executable, confirmation-free, read-only, "
                    "and declare no resource writes."
                ),
            )
        return ProactiveRuleValidation(
            accepted=True,
            code="accepted",
            message="Rule accepted for deterministic proactive execution.",
        )


class ProbeObservation(BaseModel):
    accepted: bool
    code: str
    tool_name: str
    success: bool
    summary: str
    prompt_safe_payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)


class GovernedProbeRunner:
    """Execute allowlisted proactive probes through normal tool governance."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        allowed_tool_names: set[str],
        action_validator: ActionValidator | None = None,
        tool_executor: ToolExecutor | None = None,
    ) -> None:
        if tool_executor is not None and tool_executor.registry is not registry:
            raise ValueError("GovernedProbeRunner and ToolExecutor must use the same registry")
        self.registry = registry
        self.rule_validator = ProactiveRuleValidator(
            registry=registry,
            allowed_tool_names=allowed_tool_names,
        )
        self.action_validator = action_validator or ActionValidator()
        self.tool_executor = tool_executor or ToolExecutor(registry=registry)

    def run(self, rule: WakeRule, signal: WakeSignal) -> ProbeObservation:
        rule_validation = self.rule_validator.validate(rule)
        if not rule_validation.accepted:
            return _rejected_observation(
                tool_name=rule.probe.tool_name,
                code=rule_validation.code,
                summary=rule_validation.message,
            )

        request = UserRequest(
            user_id=rule.owner.user_id,
            session_id=f"proactive:{rule.rule_id}",
            text="Explicit proactive wake rule probe.",
            metadata={
                "source": "proactive_wake",
                "tenant_id": rule.owner.tenant_id,
                "project_id": rule.owner.project_id,
                "rule_id": rule.rule_id,
                "signal_id": signal.signal_id,
            },
        )
        state = AgentState.from_request(request)
        arguments = dict(rule.probe.arguments)
        decision = AssistantDecision(
            type="tool_call",
            tool_name=rule.probe.tool_name,
            tool_input=arguments,
            reason="Explicit proactive wake rule probe.",
        )
        action_validation = self.action_validator.validate(
            decision=decision,
            registry=self.registry,
            request=request,
            state=state,
        )
        if not action_validation.accepted:
            return _rejected_observation(
                tool_name=rule.probe.tool_name,
                code=action_validation.code,
                summary=action_validation.message,
            )

        result = self.tool_executor.run_tool(
            state,
            "proactive_probe",
            rule.probe.tool_name,
            arguments,
            trace_id=state.trace_id,
            node_name="proactive_probe",
        )
        observation = observation_from_tool_result(result, request_text=request.text)
        compacted = compact_observation_for_context(observation.model_dump(mode="json"))
        structured_output = compacted.get("structured_output")
        output_ref = compacted.get("output_ref")
        return ProbeObservation(
            accepted=True,
            code=str(compacted.get("error_code") or compacted.get("status") or "tool_failed"),
            tool_name=rule.probe.tool_name,
            success=observation.status == "succeeded",
            summary=str(compacted.get("summary") or "Tool execution failed."),
            prompt_safe_payload=structured_output if isinstance(structured_output, dict) else {},
            source_refs=[output_ref] if isinstance(output_ref, str) and output_ref else [],
        )


def _rejected_observation(*, tool_name: str, code: str, summary: str) -> ProbeObservation:
    return ProbeObservation(
        accepted=False,
        code=code,
        tool_name=tool_name,
        success=False,
        summary=summary,
    )
