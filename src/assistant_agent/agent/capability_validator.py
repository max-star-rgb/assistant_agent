"""Validate intent decisions before planning or tool execution."""

from __future__ import annotations

import re

from assistant_agent.schemas.capabilities import CapabilityName, contract_for_intent
from assistant_agent.schemas.intent_decision import IntentDecision, PlanStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.tool_manifest import (
    ASK_FOLLOWUP_CAPABILITY,
    DIRECT_CHAT_CAPABILITY,
    IMAGE_GENERATION_CAPABILITY,
    IMAGE_UNDERSTANDING_CAPABILITY,
    MEMORY_RETRIEVAL_CAPABILITY,
    MEMORY_SAVE_CAPABILITY,
    MULTI_STEP_ORCHESTRATION_CAPABILITY,
    RENDER_3D_CAPABILITY,
    SHOPPING_SEARCH_CAPABILITY,
    VIDEO_UNDERSTANDING_CAPABILITY,
    WEB_FETCH_CAPABILITY,
    WEB_SEARCH_CAPABILITY,
)


class CapabilityValidator:
    """Apply capability input contracts to a router-produced IntentDecision."""

    url_re = re.compile(r"https?://\S+")
    render_vague_texts = {"渲染", "渲染一下", "看看效果", "做个展示", "展示一下", "3d", "3D"}

    def validate(self, decision: IntentDecision, request: UserRequest) -> IntentDecision:
        """Return a validated decision or an ask_followup decision."""

        normalized = self._normalize_decision(decision)
        missing: list[str] = []

        for capability in normalized.capabilities:
            missing.extend(self._missing_inputs_for_capability(capability, normalized, request))

        if missing:
            return self._ask_followup(normalized, missing)

        return normalized

    def _normalize_decision(self, decision: IntentDecision) -> IntentDecision:
        capabilities = list(decision.capabilities)
        if not capabilities and decision.primary_intent != ASK_FOLLOWUP_CAPABILITY:
            capabilities = [decision.primary_intent]

        if decision.primary_intent == MULTI_STEP_ORCHESTRATION_CAPABILITY and decision.plan_steps:
            capabilities = [step.capability for step in decision.plan_steps]

        plan_steps = list(decision.plan_steps)
        if not plan_steps and capabilities and capabilities != [ASK_FOLLOWUP_CAPABILITY]:
            plan_steps = [
                self._step_for_capability(index=index, capability=capability)
                for index, capability in enumerate(capabilities)
            ]

        return decision.model_copy(update={"capabilities": capabilities, "plan_steps": plan_steps})

    def _missing_inputs_for_capability(
        self,
        capability: CapabilityName,
        decision: IntentDecision,
        request: UserRequest,
    ) -> list[str]:
        if capability in {ASK_FOLLOWUP_CAPABILITY, MULTI_STEP_ORCHESTRATION_CAPABILITY}:
            return []
        if capability == DIRECT_CHAT_CAPABILITY:
            return [] if self._has_query(request) else ["text"]
        if capability == IMAGE_GENERATION_CAPABILITY:
            return [] if self._has_query(request) else ["prompt"]
        if capability == IMAGE_UNDERSTANDING_CAPABILITY:
            return [] if request.image_ids else ["image"]
        if capability == VIDEO_UNDERSTANDING_CAPABILITY:
            return [] if request.video_ids else ["video"]
        if capability == WEB_SEARCH_CAPABILITY:
            return [] if self._has_query(request) else ["query"]
        if capability == WEB_FETCH_CAPABILITY:
            return [] if self._has_url(request) else ["url"]
        if capability == SHOPPING_SEARCH_CAPABILITY:
            return [] if self._has_search_input(request) else ["search_query"]
        if capability == RENDER_3D_CAPABILITY:
            return [] if self._has_render_goal(request) else ["scene_description"]
        if capability == MEMORY_RETRIEVAL_CAPABILITY:
            missing = []
            if not getattr(request, "user_id", None):
                missing.append("user_id")
            if not getattr(request, "session_id", None):
                missing.append("session_id")
            return missing
        if capability == MEMORY_SAVE_CAPABILITY:
            missing = []
            if not self._has_query(request):
                missing.append("content")
            if not getattr(request, "user_id", None):
                missing.append("user_id")
            if not getattr(request, "session_id", None):
                missing.append("session_id")
            return missing
        return []

    def _ask_followup(self, decision: IntentDecision, missing_inputs: list[str]) -> IntentDecision:
        deduped_missing = self._dedupe(missing_inputs)
        return IntentDecision(
            primary_intent=ASK_FOLLOWUP_CAPABILITY,
            capabilities=[ASK_FOLLOWUP_CAPABILITY],
            plan_steps=[],
            missing_inputs=deduped_missing,
            confidence=decision.confidence,
            source=decision.source,
            reason=f"缺少必要输入：{', '.join(deduped_missing)}",
            matched_rules=decision.matched_rules,
            raw_output_ref=decision.raw_output_ref,
        )

    def _step_for_capability(self, index: int, capability: CapabilityName) -> PlanStep:
        contract = contract_for_intent(capability)
        return PlanStep(
            step_id=f"step_{index + 1}",
            capability=capability,
            tool_name=contract.tool_name,
            required_inputs=contract.input_requirements,
            reason=f"执行能力：{capability}",
        )

    def _has_query(self, request: UserRequest) -> bool:
        return bool((request.text or "").strip())

    def _has_url(self, request: UserRequest) -> bool:
        return bool(self.url_re.search(request.text or ""))

    def _has_search_input(self, request: UserRequest) -> bool:
        metadata = request.metadata
        return bool(
            self._has_query(request)
            or metadata.get("visual_summary")
            or metadata.get("video_summary")
        )

    def _has_render_goal(self, request: UserRequest) -> bool:
        metadata = request.metadata
        if metadata.get("scene_description") or metadata.get("render_goal"):
            return True
        text = (request.text or "").strip()
        return bool(text and text not in self.render_vague_texts)

    def _dedupe(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped
