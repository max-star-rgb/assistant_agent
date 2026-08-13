"""Map provider-neutral LLM events onto native custom-stream product facts."""

from __future__ import annotations

from typing import Any

from assistant_agent.providers.llm_events import LLMEvent
from assistant_agent.runtime.product_event_projector import (
    TextDeltaProductFact,
    new_runtime_product_fact_id,
)


RUNTIME_STREAM_PROVIDER = "runtime"


def llm_event_to_product_fact(
    event: LLMEvent,
    *,
    session_id: str,
    run_id: str | None,
    source: str,
) -> TextDeltaProductFact | None:
    """Convert one visible token occurrence to a strict product fact."""

    if event.event_type != "token_delta" or not event.text:
        return None

    payload = {
        key: value
        for key, value in dict(event.metadata).items()
        if key not in {"provider", "model", "finish_reason", "source"}
    }
    return TextDeltaProductFact(
        fact_id=new_runtime_product_fact_id("text_delta"),
        session_id=session_id,
        run_id=run_id or "unknown-run",
        text=event.text,
        provider=(
            event.provider if event.provider != RUNTIME_STREAM_PROVIDER else None
        ),
        model=event.model,
        finish_reason=event.finish_reason,
        source=source,
        payload=payload,
    )


def stream_delta_to_product_fact(
    text: str,
    payload: dict[str, Any],
    *,
    session_id: str,
    run_id: str | None,
    source: str,
) -> TextDeltaProductFact | None:
    """Adapt legacy callbacks without constructing a public ``AgentEvent``."""

    event = stream_delta_to_llm_event(text, payload)
    if event is None:
        return None
    return llm_event_to_product_fact(
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
