"""Explicit runtime phases for the foreground assistant loop."""

from enum import StrEnum


class RunPhase(StrEnum):
    """Govern whether the assistant may act or must produce a final answer."""

    ACT = "act"
    FINALIZE = "finalize"
