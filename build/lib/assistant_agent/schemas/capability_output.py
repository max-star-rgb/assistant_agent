"""Unified public output contract for assistant capabilities."""

from typing import Any, Literal

from pydantic import BaseModel, Field


CapabilityOutputStatus = Literal["succeeded", "failed", "partial", "skipped"]


class CapabilityOutputError(BaseModel):
    """Stable error item inside a capability output contract."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    detail: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False


class CapabilityOutputContract(BaseModel):
    """Stable output envelope shared by all core capabilities."""

    capability: str = Field(min_length=1)
    status: CapabilityOutputStatus
    output_ref: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[CapabilityOutputError] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def build_capability_output_contract(
    *,
    capability: str,
    status: CapabilityOutputStatus,
    output_ref: str | None = None,
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any] | CapabilityOutputError] | None = None,
    metadata: dict[str, Any] | None = None,
) -> CapabilityOutputContract:
    """Build a sanitized public capability output contract."""

    return CapabilityOutputContract(
        capability=capability,
        status=status,
        output_ref=output_ref,
        data=_without_sensitive_keys(data or {}),
        errors=[_error_item(error) for error in errors or []],
        metadata=_without_sensitive_keys(metadata or {}),
    )


def contract_summary(contract: CapabilityOutputContract | dict[str, Any] | None) -> dict[str, Any]:
    """Return a small event-safe summary of a capability contract."""

    if contract is None:
        return {}
    payload = contract.model_dump(mode="json") if isinstance(contract, CapabilityOutputContract) else contract
    return {
        "capability": payload.get("capability"),
        "status": payload.get("status"),
        "output_ref": payload.get("output_ref"),
        "error_count": len(payload.get("errors") or []),
    }


def _error_item(error: dict[str, Any] | CapabilityOutputError) -> CapabilityOutputError:
    if isinstance(error, CapabilityOutputError):
        return error
    return CapabilityOutputError(
        code=str(error.get("code", "unknown_error")),
        message=str(error.get("message", error.get("msg", "Capability failed."))),
        detail=_without_sensitive_keys(error.get("detail") or {}),
        recoverable=bool(error.get("recoverable", False)),
    )


def _without_sensitive_keys(payload: dict[str, Any]) -> dict[str, Any]:
    blocked = {"api_key", "authorization", "bearer", "provider_response", "raw", "base64"}
    return {key: value for key, value in payload.items() if key.lower() not in blocked}
