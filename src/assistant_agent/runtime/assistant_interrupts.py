"""Strict contracts for native AssistantTurnGraph interrupt and resume."""

from __future__ import annotations

import re
import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)


AssistantInterruptKind = Literal["approval", "input"]
AssistantResumeKind = Literal["approve", "reject", "provide_input"]

_ACTION_REF_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_SENSITIVE_PROMPT_PATTERN = re.compile(
    r"(?i)(?:authorization\s*:|bearer\s+[A-Za-z0-9._~-]+|cookie\s*:|"
    r"password\s*[:=]|-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----)"
)


def _validate_public_prompt(value: str) -> str:
    if any(ord(char) < 32 and char not in {"\n", "\r", "\t"} for char in value):
        raise ValueError("interrupt prompt contains control characters")
    if _SENSITIVE_PROMPT_PATTERN.search(value) is not None:
        raise ValueError("interrupt prompt contains credential-like content")
    return value


def _validate_action_ref(value: str) -> str:
    if _ACTION_REF_PATTERN.fullmatch(value) is None:
        raise ValueError("action_ref must be a bounded stable business ref")
    return value


class AssistantInterruptContractError(ValueError):
    """Structured fail-closed error for interrupt/resume contract violations."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AssistantInterruptRequest(_StrictContract):
    """Trusted request to pause one graph action at a stable business ref."""

    schema_version: Literal[1] = 1
    kind: AssistantInterruptKind
    prompt: str = Field(min_length=1, max_length=2_000)
    action_ref: str = Field(min_length=1, max_length=256)
    allowed_resume_kinds: tuple[AssistantResumeKind, ...] = Field(
        min_length=1,
        max_length=2,
    )

    @field_validator("action_ref")
    @classmethod
    def validate_action_ref(cls, value: str) -> str:
        return _validate_action_ref(value)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return _validate_public_prompt(value)

    @model_validator(mode="after")
    def validate_allowed_resume_kinds(self) -> "AssistantInterruptRequest":
        allowed = tuple(dict.fromkeys(self.allowed_resume_kinds))
        expected = (
            frozenset({"approve", "reject"})
            if self.kind == "approval"
            else frozenset({"provide_input"})
        )
        if len(allowed) != len(self.allowed_resume_kinds) or not set(allowed).issubset(
            expected
        ):
            raise ValueError(
                f"{self.kind} interrupt has incompatible allowed resume kinds"
            )
        return self


class _AssistantResumeBase(_StrictContract):
    schema_version: Literal[1] = 1
    action_ref: str = Field(min_length=1, max_length=256)

    @field_validator("action_ref")
    @classmethod
    def validate_action_ref(cls, value: str) -> str:
        return _validate_action_ref(value)


class AssistantApproveResume(_AssistantResumeBase):
    kind: Literal["approve"] = "approve"


class AssistantRejectResume(_AssistantResumeBase):
    kind: Literal["reject"] = "reject"
    reason: str | None = Field(default=None, min_length=1, max_length=1_000)


class AssistantInputResume(_AssistantResumeBase):
    kind: Literal["provide_input"] = "provide_input"
    text: str = Field(min_length=1, max_length=8_000)


AssistantResume = Annotated[
    AssistantApproveResume | AssistantRejectResume | AssistantInputResume,
    Field(discriminator="kind"),
]
_RESUME_ADAPTER = TypeAdapter(AssistantResume)


class AssistantInterrupt(_StrictContract):
    """Public-safe interrupt projected from a native LangGraph Interrupt."""

    schema_version: Literal[1] = 1
    interrupt_id: str = Field(min_length=1, max_length=256)
    kind: AssistantInterruptKind
    prompt: str = Field(min_length=1, max_length=2_000)
    action_ref: str = Field(min_length=1, max_length=256)
    allowed_resume_kinds: tuple[AssistantResumeKind, ...] = Field(
        min_length=1,
        max_length=2,
    )

    @field_validator("action_ref")
    @classmethod
    def validate_action_ref(cls, value: str) -> str:
        return _validate_action_ref(value)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return _validate_public_prompt(value)


def assistant_turn_action_ref(run_id: str) -> str:
    """Return the canonical stable ref used by an input interrupt."""

    ref = f"assistant-turn:{run_id}"
    if _ACTION_REF_PATTERN.fullmatch(ref) is None:
        raise AssistantInterruptContractError(
            "interrupt_action_ref_invalid",
            "Assistant turn cannot be represented as a stable action ref.",
        )
    return ref


def validate_assistant_interrupt_request(
    value: object,
) -> AssistantInterruptRequest:
    """Validate checkpoint JSON semantics without accepting loose Python coercion."""

    try:
        return AssistantInterruptRequest.model_validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise AssistantInterruptContractError(
            "assistant_interrupt_invalid",
            "Persisted assistant interrupt request is invalid.",
        ) from exc


def validate_assistant_resume(
    value: object,
) -> AssistantApproveResume | AssistantRejectResume | AssistantInputResume:
    """Validate an untrusted native resume payload as a strict union."""

    try:
        return _RESUME_ADAPTER.validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise AssistantInterruptContractError(
            "assistant_resume_invalid",
            "Assistant resume payload is invalid.",
        ) from exc


def validate_resume_for_interrupt(
    request: AssistantInterruptRequest,
    resume: AssistantApproveResume | AssistantRejectResume | AssistantInputResume,
) -> None:
    """Bind a resume to the exact persisted action and allowed resume set."""

    if resume.action_ref != request.action_ref:
        raise AssistantInterruptContractError(
            "assistant_resume_action_ref_mismatch",
            "Assistant resume action_ref does not match the pending interrupt.",
        )
    if resume.kind not in request.allowed_resume_kinds:
        raise AssistantInterruptContractError(
            "assistant_resume_kind_not_allowed",
            "Assistant resume kind is not allowed for the pending interrupt.",
        )


__all__ = [
    "AssistantApproveResume",
    "AssistantInputResume",
    "AssistantInterrupt",
    "AssistantInterruptContractError",
    "AssistantInterruptKind",
    "AssistantInterruptRequest",
    "AssistantRejectResume",
    "AssistantResume",
    "AssistantResumeKind",
    "assistant_turn_action_ref",
    "validate_assistant_interrupt_request",
    "validate_assistant_resume",
    "validate_resume_for_interrupt",
]
