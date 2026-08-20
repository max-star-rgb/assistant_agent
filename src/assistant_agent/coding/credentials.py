"""Credential approval contracts without serializing credential material."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Mapping

from pydantic import ValidationError

from assistant_agent.coding.config import (
    CodingCredentialProfile,
    CodingDependencyProfile,
)
from assistant_agent.coding.models import (
    CodingCredentialApprovalDecision,
    CodingCredentialRequest,
    CodingDependencyPlan,
)


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def build_credential_request(
    dependency: CodingDependencyProfile,
    credential: CodingCredentialProfile,
    plan: CodingDependencyPlan,
) -> CodingCredentialRequest:
    if (
        dependency.credential_profile_id != credential.credential_profile_id
        or dependency.profile_id != plan.profile_id
        or credential.registry_host not in plan.allowed_hosts
    ):
        raise ValueError("credential_approval_mismatch")
    credential_policy = {
        "credential_profile_id": credential.credential_profile_id,
        "registry_host": credential.registry_host,
        "registry_base_path": credential.registry_base_path,
        "auth_scheme": credential.auth_scheme,
        "lease_ttl_seconds": credential.lease_ttl_seconds,
        "gateway_image": credential.gateway_image,
    }
    values = {
        "credential_profile_id": credential.credential_profile_id,
        "dependency_profile_id": dependency.profile_id,
        "registry_host": credential.registry_host,
        "registry_base_path": credential.registry_base_path,
        "lease_ttl_seconds": credential.lease_ttl_seconds,
        "dependency_plan_digest": plan.plan_digest,
        "dependency_policy_digest": plan.policy_digest,
        "credential_policy_digest": _digest(credential_policy),
    }
    return CodingCredentialRequest(**values, request_digest=_digest(values))


def credential_interrupt_payload(request: CodingCredentialRequest) -> dict[str, object]:
    return {
        "action": "coding_credential_lease",
        "credential": request.model_dump(mode="json"),
    }


def validate_credential_approval(
    request: CodingCredentialRequest,
    raw: object,
) -> Literal["approve", "reject"]:
    if not isinstance(raw, Mapping):
        raise ValueError("credential_approval_mismatch")
    try:
        decision = CodingCredentialApprovalDecision.model_validate(dict(raw))
    except ValidationError as exc:
        raise ValueError("credential_approval_mismatch") from exc
    if decision.decision == "approve" and decision.request_digest != request.request_digest:
        raise ValueError("credential_approval_mismatch")
    return decision.decision


__all__ = [
    "build_credential_request",
    "credential_interrupt_payload",
    "validate_credential_approval",
]
