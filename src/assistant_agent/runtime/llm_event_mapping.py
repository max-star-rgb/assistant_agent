"""Map provider-neutral LLM events onto runtime AgentEvent records."""

from __future__ import annotations

from typing import Any

from assistant_agent.runtime.events import AgentEvent
from assistant_agent.providers.llm_events import LLMEvent


RUNTIME_STREAM_PROVIDER = "runtime"


def llm_event_to_agent_event(
    event: LLMEvent,
    *,
    session_id: str,
    run_id: str | None,
    source: str,
) -> AgentEvent | None:
    """Convert user-visible token deltas to the existing runtime event shape."""

    if event.event_type != "token_delta" or not event.text:
        return None

    payload = dict(event.metadata)
    if event.provider != RUNTIME_STREAM_PROVIDER:
        payload["provider"] = event.provider
    if event.model is not None:
        payload["model"] = event.model
    if event.finish_reason is not None:
        payload["finish_reason"] = event.finish_reason
    payload["source"] = source

    return AgentEvent(
        type="response_delta",
        session_id=session_id,
        run_id=run_id,
        text=event.text,
        payload=payload,
    )


def stream_delta_to_agent_event(
    text: str,
    payload: dict[str, Any],
    *,
    session_id: str,
    run_id: str | None,
    source: str,
) -> AgentEvent | None:
    """Adapt legacy stream callbacks through the LLMEvent mapping boundary."""

    event = stream_delta_to_llm_event(text, payload)
    if event is None:
        return None
    return llm_event_to_agent_event(
        event,
        session_id=session_id,
        run_id=run_id,
        source=source,
    )


def stream_delta_to_llm_event(text: str, payload: dict[str, Any]) -> LLMEvent | None:
    """Normalize a legacy text callback payload into a provider-neutral token event."""

    if not text:
        return None
    metadata = dict(payload)
    provider = _metadata_text(metadata.get("provider")) or RUNTIME_STREAM_PROVIDER
    model = _metadata_text(metadata.get("model"))
    finish_reason = _metadata_text(metadata.get("finish_reason"))
    return LLMEvent(
        event_type="token_delta",
        provider=provider,
        model=model,
        text=text,
        finish_reason=finish_reason,
        metadata=metadata,
    )


def _metadata_text(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
