"""Shared normalization for persisted LangSmith Boolean Feedback scores."""

from __future__ import annotations

import math
from typing import Any


def normalize_boolean_feedback_score(value: Any) -> bool:
    """Accept SDK booleans or finite numeric 0/1 and reject every other value."""

    if isinstance(value, bool):
        return value
    if type(value) in (int, float) and math.isfinite(value) and value in (0, 1):
        return bool(value)
    raise ValueError("persisted Feedback score is not Boolean")


__all__ = ["normalize_boolean_feedback_score"]
