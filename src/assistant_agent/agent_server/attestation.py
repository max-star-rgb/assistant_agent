"""Non-secret attestation for one actual Agent Server composition."""

from __future__ import annotations

from hashlib import sha256
import json
import re
import secrets
import unicodedata
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, field_validator

from assistant_agent.agent_server.config import ASSISTANT_GRAPH_ID
from assistant_agent.coding.config import CodingConfig, CodingRepositoryConfig
from assistant_agent.config import ProviderConfig


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}$")
_SAFE_REPOSITORY_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")


class AgentServerExecutionAttestation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    graph_id: Literal["assistant-native-v3"]
    provider_mode: Literal["mock", "real"]
    chat_provider: str
    chat_adapter: str
    model_id: str
    coding_enabled: bool
    coding_registry_digest: str
    repository_config_digests: dict[str, str]
    process_boot_nonce: str

    @field_validator("chat_provider", "chat_adapter", "model_id")
    @classmethod
    def _safe_identifier(cls, value: str) -> str:
        if (
            unicodedata.normalize("NFC", value) != value
            or _SAFE_IDENTIFIER.fullmatch(value) is None
            or ".." in value
        ):
            raise ValueError("attestation identifier is unsafe")
        return value

    @field_validator("coding_registry_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _HEX_64.fullmatch(value) is None:
            raise ValueError("attestation digest is invalid")
        return value

    @field_validator("repository_config_digests")
    @classmethod
    def _repository_digests(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32 or tuple(value) != tuple(sorted(value)):
            raise ValueError("attestation repository inventory is noncanonical")
        if any(
            _SAFE_REPOSITORY_ID.fullmatch(key) is None
            or _HEX_64.fullmatch(digest) is None
            for key, digest in value.items()
        ):
            raise ValueError("attestation repository inventory is invalid")
        return value

    @field_validator("process_boot_nonce")
    @classmethod
    def _nonce(cls, value: str) -> str:
        if _HEX_32.fullmatch(value) is None:
            raise ValueError("attestation boot nonce is invalid")
        return value

    def canonical_digest(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


def coding_repository_config_digest(repository: CodingRepositoryConfig) -> str:
    payload = repository.model_dump(mode="json")
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def execution_attestation_digest(
    attestation: AgentServerExecutionAttestation,
) -> str:
    """Return the stable composition digest without coupling callers to serialization."""

    return attestation.canonical_digest()


def coding_registry_digest(
    repositories: Mapping[str, CodingRepositoryConfig],
) -> tuple[str, dict[str, str]]:
    digests = {
        repo_id: coding_repository_config_digest(repository)
        for repo_id, repository in sorted(repositories.items())
    }
    digest = sha256(
        json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest, digests


def build_execution_attestation(
    provider: ProviderConfig,
    coding: CodingConfig,
) -> AgentServerExecutionAttestation:
    resolved = provider.resolved_chat_provider()
    registry_digest, repository_digests = coding_registry_digest(coding.repositories)
    return AgentServerExecutionAttestation(
        schema_version=1,
        graph_id=ASSISTANT_GRAPH_ID,
        provider_mode=provider.provider_mode,
        chat_provider=provider.chat_provider,
        chat_adapter=provider.chat_adapter_kind,
        model_id=resolved.model or "unconfigured",
        coding_enabled=coding.enabled,
        coding_registry_digest=registry_digest,
        repository_config_digests=repository_digests,
        process_boot_nonce=secrets.token_hex(16),
    )


__all__ = [
    "AgentServerExecutionAttestation",
    "build_execution_attestation",
    "coding_registry_digest",
    "coding_repository_config_digest",
    "execution_attestation_digest",
]
