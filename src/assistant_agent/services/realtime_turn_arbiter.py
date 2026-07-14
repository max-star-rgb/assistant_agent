"""Independent LLM boundary for realtime semantic turn arbitration."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.realtime_turn_arbitration import (
    RealtimeTurnArbitrationDecision,
    RealtimeTurnArbitrationRequest,
    normalize_arbitration_decision,
    prompt_safe_arbitration_task_state,
    uncertain_arbitration_decision,
)
from assistant_agent.services.chat_adapter import (
    ChatAdapter,
    ChatRequest,
    UnconfiguredChatAdapter,
)


_ARBITRATION_SYSTEM_INSTRUCTION = """You are a realtime turn arbiter, not a business assistant.
Classify how the new utterance relates to the active task. Do not call tools, solve the task,
or repeat private context. Return one JSON object with disposition, optional revision_type,
confidence from 0 to 1, and a short lowercase reason_code. Allowed dispositions are FOLLOWUP,
CANCEL_ONLY, REVISE_ACTIVE, REPLACE_ACTIVE, ACK_NOOP, and UNCERTAIN. Prefer UNCERTAIN when
evidence is insufficient; never infer explicit media control from text alone."""


class RealtimeTurnArbiter(Protocol):
    """Provider-neutral semantic control-plane boundary."""

    async def arbitrate(
        self,
        request: RealtimeTurnArbitrationRequest,
    ) -> RealtimeTurnArbitrationDecision:
        """Return one structured decision without running business tools."""


class ConservativeRealtimeTurnArbiter:
    """Safe default used when real semantic arbitration is unavailable."""

    async def arbitrate(
        self,
        request: RealtimeTurnArbitrationRequest,
    ) -> RealtimeTurnArbitrationDecision:
        return uncertain_arbitration_decision(
            request,
            fallback_reason="llm_arbiter_disabled",
        )


class ChatAdapterRealtimeTurnArbiter:
    """Small structured LLM call independent from the business agent loop."""

    def __init__(self, chat_adapter: ChatAdapter, *, min_confidence: float = 0.80) -> None:
        self.chat_adapter = chat_adapter
        self.min_confidence = min_confidence

    async def arbitrate(
        self,
        request: RealtimeTurnArbitrationRequest,
    ) -> RealtimeTurnArbitrationDecision:
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(
                self.chat_adapter.chat,
                ChatRequest(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    user_query=_arbitration_prompt(request),
                    system_instruction=_ARBITRATION_SYSTEM_INSTRUCTION,
                    response_format={"type": "json_object"},
                    temperature=0.0,
                    max_tokens=256,
                ),
            )
        except Exception:
            return uncertain_arbitration_decision(
                request,
                source="semantic_llm",
                fallback_reason="provider_error",
                latency_ms=_elapsed_ms(started),
            )
        if not result.success:
            return uncertain_arbitration_decision(
                request,
                source="semantic_llm",
                fallback_reason="provider_error",
                latency_ms=_elapsed_ms(started),
            )
        try:
            payload = _extract_json_object(result.response_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return uncertain_arbitration_decision(
                request,
                source="semantic_llm",
                fallback_reason="invalid_model_output",
                latency_ms=_elapsed_ms(started),
            )
        return normalize_arbitration_decision(
            payload,
            request=request,
            min_confidence=self.min_confidence,
            source="semantic_llm",
            latency_ms=_elapsed_ms(started),
        )


def create_realtime_turn_arbiter(
    config: ProviderConfig,
    chat_adapter: ChatAdapter,
    *,
    min_confidence: float = 0.80,
) -> RealtimeTurnArbiter:
    """Create an LLM arbiter only inside explicit real-provider profiles."""

    provider = str(getattr(chat_adapter, "provider", "") or "").strip().lower()
    if (
        config.runtime_profile.name not in {"provider_smoke", "pilot"}
        or provider in {"", "mock"}
        or isinstance(chat_adapter, UnconfiguredChatAdapter)
    ):
        return ConservativeRealtimeTurnArbiter()
    return ChatAdapterRealtimeTurnArbiter(
        chat_adapter,
        min_confidence=min_confidence,
    )


def _arbitration_prompt(request: RealtimeTurnArbitrationRequest) -> str:
    payload = {
        "utterance": request.utterance,
        "language": request.language,
        "active_task": prompt_safe_arbitration_task_state(request.task_state),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _extract_json_object(text: str) -> Mapping[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model output does not contain a JSON object")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, Mapping):
        raise TypeError("model output must be a JSON object")
    return payload


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
