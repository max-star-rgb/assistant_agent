"""Governed read-only tool probes for proactive wake rules."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.runtime.decision_models import AssistantDecision
from assistant_agent.automation.proactive_wake.models import WakeRule, WakeSignal
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.observation import observation_from_tool_result
from assistant_agent.context.compaction import compact_observation_for_context
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
        if spec.category != "read":
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_tool_not_read_only",
                message="Probe tool must be read-only and declare no resource writes.",
            )
        try:
            tool = self.registry.get(rule.probe.tool_name)
            tool.input_schema.model_validate(rule.probe.arguments)
        except ValidationError:
            return ProactiveRuleValidation(
                accepted=False,
                code="proactive_probe_arguments_invalid",
                message="Probe arguments do not match the registered tool input schema.",
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
                "agent_id": rule.owner.agent_id,
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
        observation = observation_from_tool_result(result)
        compacted = compact_observation_for_context(observation.model_dump(mode="json"))
        data = compacted.get("data")
        error = compacted.get("error")
        output_ref = compacted.get("output_ref")
        return ProbeObservation(
            accepted=True,
            code=str(
                error.get("code")
                if isinstance(error, dict)
                else compacted.get("status") or "tool_failed"
            ),
            tool_name=rule.probe.tool_name,
            success=observation.status == "succeeded",
            summary=str(compacted.get("summary") or "Tool execution failed."),
            prompt_safe_payload=data if isinstance(data, dict) else {},
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
