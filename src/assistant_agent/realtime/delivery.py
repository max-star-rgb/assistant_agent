"""Stable delivery semantics for realtime runtime events."""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict


SpeechPolicy = Literal["never", "optional", "required"]
PersistencePolicy = Literal["ephemeral", "final"]


class RealtimeEventDeliveryPolicy(BaseModel):
    """Entry-layer delivery facts for one semantic event type."""

    model_config = ConfigDict(frozen=True)

    speech_policy: SpeechPolicy
    persistence: PersistencePolicy
    replaceable: bool = False


_DELIVERY_POLICIES: dict[str, RealtimeEventDeliveryPolicy] = {
    "response.chunk": RealtimeEventDeliveryPolicy(
        speech_policy="required",
        persistence="final",
    ),
    "run.progress": RealtimeEventDeliveryPolicy(
        speech_policy="optional",
        persistence="ephemeral",
        replaceable=True,
    ),
    "tool.started": RealtimeEventDeliveryPolicy(
        speech_policy="never",
        persistence="ephemeral",
    ),
    "tool.finished": RealtimeEventDeliveryPolicy(
        speech_policy="never",
        persistence="ephemeral",
    ),
    "tool.failed": RealtimeEventDeliveryPolicy(
        speech_policy="never",
        persistence="ephemeral",
    ),
    "confirmation.required": RealtimeEventDeliveryPolicy(
        speech_policy="required",
        persistence="ephemeral",
    ),
}


def progress_replacement_key(run_id: str) -> str:
    """Return the stable replaceable slot used by one run's progress events."""

    return f"{run_id}:progress"


def apply_realtime_delivery_policy(
    payload: Mapping[str, Any],
    *,
    event_type: str,
    run_id: str,
) -> dict[str, Any]:
    """Attach stable delivery facts without trusting producer overrides."""

    mapped = dict(payload)
    policy = _DELIVERY_POLICIES[event_type]
    mapped["speech_policy"] = policy.speech_policy
    mapped["persistence"] = policy.persistence
    mapped["replaceable"] = policy.replaceable
    if event_type == "run.progress":
        mapped["replacement_key"] = progress_replacement_key(run_id)
    elif event_type == "response.chunk":
        mapped["supersedes"] = [progress_replacement_key(run_id)]
    return mapped
