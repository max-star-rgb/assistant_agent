"""Validate intent decisions before planning or tool execution."""

from __future__ import annotations

from assistant_agent.schemas.capabilities import CapabilityName, contract_for_intent
from assistant_agent.schemas.intent_decision import IntentDecision, PlanStep
from assistant_agent.schemas.requests import UserRequest


class CapabilityValidator:
    """Apply capability input contracts to a router-produced IntentDecision."""

    render_vague_texts = {"渲染", "渲染一下", "看看效果", "做个展示", "展示一下", "3d", "3D"}

    def validate(self, decision: IntentDecision, request: UserRequest) -> IntentDecision:
        """Return a validated decision or an ask_followup decision."""

        normalized = self._normalize_decision(decision)
        missing: list[str] = []

        for capability in normalized.capabilities:
            missing.extend(self._missing_inputs_for_capability(capability, normalized, request))

        if missing:
            return self._ask_followup(normalized, missing)

        if "price_compare" in normalized.capabilities and not self._has_product_candidates(normalized, request):
            if self._has_query(request):
                return self._ensure_search_before_price_compare(normalized)
            return self._ask_followup(normalized, ["product_candidates", "search_query"])

        return normalized

    def _normalize_decision(self, decision: IntentDecision) -> IntentDecision:
        capabilities = list(decision.capabilities)
        if not capabilities and decision.primary_intent != "ask_followup":
            capabilities = [decision.primary_intent]

        if decision.primary_intent == "multi_step_orchestration" and decision.plan_steps:
            capabilities = [step.capability for step in decision.plan_steps]

        plan_steps = list(decision.plan_steps)
        if not plan_steps and capabilities and capabilities != ["ask_followup"]:
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
        if capability in {"ask_followup", "multi_step_orchestration"}:
            return []
        if capability == "direct_chat":
            return [] if self._has_query(request) else ["text"]
        if capability == "image_generation":
            return [] if self._has_query(request) else ["prompt"]
        if capability == "image_understanding":
            return [] if request.image_ids else ["image"]
        if capability == "video_understanding":
            return [] if request.video_ids else ["video"]
        if capability == "product_search":
            return [] if self._has_search_input(request) else ["search_query"]
        if capability == "price_compare":
            if self._has_product_candidates(decision, request) or self._has_query(request):
                return []
            return ["product_candidates", "search_query"]
        if capability == "render_3d":
            return [] if self._has_render_goal(request) else ["scene_description"]
        if capability == "memory_retrieval":
            missing = []
            if not getattr(request, "user_id", None):
                missing.append("user_id")
            if not getattr(request, "session_id", None):
                missing.append("session_id")
            return missing
        if capability == "memory_save":
            missing = []
            if not self._has_query(request):
                missing.append("content")
            if not getattr(request, "user_id", None):
                missing.append("user_id")
            if not getattr(request, "session_id", None):
                missing.append("session_id")
            return missing
        return []

    def _ensure_search_before_price_compare(self, decision: IntentDecision) -> IntentDecision:
        capabilities: list[CapabilityName] = []
        for capability in decision.capabilities:
            if capability == "price_compare" and "product_search" not in capabilities:
                capabilities.append("product_search")
            if capability not in capabilities:
                capabilities.append(capability)

        plan_steps = [
            self._step_for_capability(index=index, capability=capability)
            for index, capability in enumerate(capabilities)
        ]
        return decision.model_copy(
            update={
                "primary_intent": "multi_step_orchestration",
                "capabilities": capabilities,
                "plan_steps": plan_steps,
                "reason": decision.reason or "比价缺少候选商品，先搜索商品再比价。",
            }
        )

    def _ask_followup(self, decision: IntentDecision, missing_inputs: list[str]) -> IntentDecision:
        deduped_missing = self._dedupe(missing_inputs)
        return IntentDecision(
            primary_intent="ask_followup",
            capabilities=["ask_followup"],
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

    def _has_search_input(self, request: UserRequest) -> bool:
        metadata = request.metadata
        return bool(
            self._has_query(request)
            or metadata.get("visual_summary")
            or metadata.get("video_summary")
        )

    def _has_product_candidates(self, decision: IntentDecision, request: UserRequest) -> bool:
        metadata = request.metadata
        if metadata.get("product_candidates") or metadata.get("products"):
            return True
        return any(step.capability == "product_search" for step in decision.plan_steps)

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
