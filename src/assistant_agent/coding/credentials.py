"""Credential approval contracts without serializing credential material."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

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


def _credential_policy_digest(credential: CodingCredentialProfile) -> str:
    return _digest(
        {
            "credential_profile_id": credential.credential_profile_id,
            "registry_host": credential.registry_host,
            "registry_base_path": credential.registry_base_path,
            "auth_scheme": credential.auth_scheme,
            "lease_ttl_seconds": credential.lease_ttl_seconds,
            "gateway_image": credential.gateway_image,
        }
    )


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
    values = {
        "credential_profile_id": credential.credential_profile_id,
        "dependency_profile_id": dependency.profile_id,
        "registry_host": credential.registry_host,
        "registry_base_path": credential.registry_base_path,
        "lease_ttl_seconds": credential.lease_ttl_seconds,
        "dependency_plan_digest": plan.plan_digest,
        "dependency_policy_digest": plan.policy_digest,
        "credential_policy_digest": _credential_policy_digest(credential),
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


@dataclass(slots=True)
class CredentialLease:
    """Non-serializable, process-local credential material with explicit zeroization."""

    lease_id: str
    credential_profile_id: str
    request_digest: str
    registry_host: str
    registry_base_path: str
    issued_at: datetime
    expires_at: datetime
    secret: bytearray
    closed: bool = False

    def close(self) -> None:
        for index in range(len(self.secret)):
            self.secret[index] = 0
        self.closed = True


class CredentialBroker(Protocol):
    def acquire(self, request: CodingCredentialRequest) -> CredentialLease: ...


class EnvironmentCredentialBroker:
    """Resolve operator-owned secret env names only after HITL approval."""

    def __init__(
        self,
        profiles: Mapping[str, CodingCredentialProfile],
        *,
        env: Mapping[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        lease_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._profiles = dict(profiles)
        self._env = os.environ if env is None else env
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lease_id_factory = lease_id_factory or (lambda: uuid.uuid4().hex)

    def acquire(self, request: CodingCredentialRequest) -> CredentialLease:
        profile = self._profiles.get(request.credential_profile_id)
        if profile is None:
            raise ValueError("credential_broker_unconfigured")
        if (
            request.registry_host != profile.registry_host
            or request.registry_base_path != profile.registry_base_path
            or request.lease_ttl_seconds != profile.lease_ttl_seconds
            or request.credential_policy_digest != _credential_policy_digest(profile)
        ):
            raise ValueError("credential_approval_mismatch")
        raw = self._env.get(profile.secret_env, "")
        if (
            not raw
            or len(raw.encode("utf-8")) > 16_384
            or any(character in raw for character in ("\x00", "\n", "\r"))
        ):
            raise ValueError("credential_broker_unconfigured")
        secret = bytearray(raw, "utf-8")
        issued_at = self._now()
        if issued_at.tzinfo is None:
            for index in range(len(secret)):
                secret[index] = 0
            raise ValueError("credential_broker_unconfigured")
        return CredentialLease(
            lease_id=self._lease_id_factory(),
            credential_profile_id=profile.credential_profile_id,
            request_digest=request.request_digest,
            registry_host=profile.registry_host,
            registry_base_path=profile.registry_base_path,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=profile.lease_ttl_seconds),
            secret=secret,
        )


@contextmanager
def credential_lease(
    broker: CredentialBroker,
    request: CodingCredentialRequest,
) -> Iterator[CredentialLease]:
    lease = broker.acquire(request)
    try:
        yield lease
    finally:
        lease.close()


__all__ = [
    "build_credential_request",
    "CredentialBroker",
    "CredentialLease",
    "EnvironmentCredentialBroker",
    "credential_lease",
    "credential_interrupt_payload",
    "validate_credential_approval",
]
