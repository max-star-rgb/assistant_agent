"""Canonical proactive wake evidence fingerprinting."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from assistant_agent.schemas.proactive_wake import WakeEvidence, WakeRule, WakeRuleState
from assistant_agent.services.proactive_wake.probe import ProbeObservation


def evidence_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_wake_evidence(
    *,
    rule: WakeRule,
    observation: ProbeObservation,
    state: WakeRuleState,
    observed_at: datetime,
) -> WakeEvidence:
    fingerprint = evidence_fingerprint(observation.prompt_safe_payload)
    previous = state.last_fingerprint
    return WakeEvidence(
        rule_id=rule.rule_id,
        observed_at=observed_at,
        probe_tool_name=observation.tool_name,
        status="succeeded" if observation.success else "failed",
        fingerprint=fingerprint,
        previous_fingerprint=previous,
        is_initial=previous is None,
        changed=previous is not None and previous != fingerprint,
        summary=observation.summary[:500],
        prompt_safe_payload=observation.prompt_safe_payload,
        source_refs=observation.source_refs,
    )
