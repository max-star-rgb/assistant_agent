"""Built-in Mem0 implementation of ``assistant_memory_plugin_v1``."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from assistant_agent.config import ProviderConfig
from assistant_agent.memory.mem0.client import Mem0Client, UnavailableMem0Client
from assistant_agent.memory.mem0.models import (
    Mem0CompletedTurn,
    Mem0Identity,
    Mem0IngestionResult,
)
from assistant_agent.memory.models import LongTermMemory
from assistant_agent.memory.plugins.contracts import (
    MemoryChange,
    MemoryContextContribution,
    MemoryContextItem,
    MemoryContextRequest,
    MemoryPluginCapabilities,
    MemoryPluginBuildContext,
    MemoryPluginDescriptor,
    MemoryPluginIssue,
    MemorySessionCloseRequest,
    MemorySessionCloseResult,
    MemorySessionOpenRequest,
    MemorySessionOpenResult,
    MemoryTurnIngestionRequest,
    MemoryTurnIngestionResult,
)


MEM0_MEMORY_PLUGIN_DESCRIPTOR = MemoryPluginDescriptor(
    plugin_id="mem0",
    plugin_version="1",
    capabilities=MemoryPluginCapabilities(
        modalities={"text"},
        supports_session_recall=True,
        supports_turn_ingestion=True,
        supports_context_refresh=False,
        supports_idempotent_ingestion=True,
    ),
)


class Mem0MemoryPluginConfig(BaseModel):
    """Validated, secret-safe inputs for constructing the Mem0 adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: SecretStr | str | None = None
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=5.0, gt=0.0)


class _Mem0ClientAdapter(Protocol):
    def recall_long_term_memory(
        self,
        identity: Mem0Identity,
    ) -> list[LongTermMemory]: ...

    def ingest_completed_turn(
        self,
        turn: Mem0CompletedTurn,
    ) -> Mem0IngestionResult: ...


class Mem0MemoryPlugin:
    """Translate the standard Memory Plugin API to the private Mem0 client."""

    descriptor = MEM0_MEMORY_PLUGIN_DESCRIPTOR

    def __init__(self, *, client: _Mem0ClientAdapter) -> None:
        self._client = client

    def open_session(
        self,
        request: MemorySessionOpenRequest,
    ) -> MemorySessionOpenResult:
        request.cancellation.raise_if_cancelled()
        identity = Mem0Identity(
            user_id=request.identity.user_id,
            agent_id=request.identity.agent_id,
            run_id=request.identity.session_id,
        )
        try:
            memories = self._client.recall_long_term_memory(identity)
        except Exception:
            return MemorySessionOpenResult(
                status="unavailable",
                initial_contribution=MemoryContextContribution(
                    status="unavailable"
                ),
                issues=[_issue("mem0_recall_failed", recoverable=True)],
            )
        return MemorySessionOpenResult(
            status="ready",
            initial_contribution=MemoryContextContribution(
                items=[
                    MemoryContextItem.model_validate(
                        memory.model_dump(mode="python")
                    )
                    for memory in memories
                ],
                status="succeeded",
            ),
        )

    def ingest_turn(
        self,
        request: MemoryTurnIngestionRequest,
    ) -> MemoryTurnIngestionResult:
        request.cancellation.raise_if_cancelled()
        identity = Mem0Identity(
            user_id=request.identity.user_id,
            agent_id=request.identity.agent_id,
            run_id=request.identity.session_id,
        )
        try:
            native = self._client.ingest_completed_turn(
                Mem0CompletedTurn(
                    identity=identity,
                    user_text=request.turn.user_message.text,
                    assistant_text=request.turn.assistant_message.text,
                    occurred_at=request.turn.occurred_at,
                    source_turn=request.idempotency_key,
                )
            )
        except Exception:
            return MemoryTurnIngestionResult(
                status="failed",
                issues=[_issue("mem0_ingestion_failed", recoverable=True)],
            )
        operations = {
            "ADD": "created",
            "UPDATE": "updated",
            "DELETE": "deleted",
        }
        status = (
            "failed"
            if not native.accepted
            else "partial"
            if native.errors
            else "accepted"
        )
        return MemoryTurnIngestionResult(
            status=status,
            changes=[
                MemoryChange(
                    operation=operations[change.event],
                    memory_id=change.memory_id,
                    memory_type="long_term",
                )
                for change in native.changes or []
            ],
            issues=(
                [_issue("mem0_ingestion_failed", recoverable=True)]
                if native.errors or not native.accepted
                else []
            ),
        )

    def prepare_context(
        self,
        request: MemoryContextRequest,
    ) -> MemoryContextContribution:
        request.cancellation.raise_if_cancelled()
        return MemoryContextContribution(status="succeeded")

    def close_session(
        self,
        request: MemorySessionCloseRequest,
    ) -> MemorySessionCloseResult:
        request.cancellation.raise_if_cancelled()
        return MemorySessionCloseResult(status="closed")


class Mem0MemoryPluginFactory:
    """Construct the built-in Plugin without connecting to Mem0 at import."""

    descriptor = MEM0_MEMORY_PLUGIN_DESCRIPTOR
    config_model = Mem0MemoryPluginConfig

    def __init__(
        self,
        *,
        defaults: Mem0MemoryPluginConfig | None = None,
    ) -> None:
        self._defaults = defaults or Mem0MemoryPluginConfig()

    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> Mem0MemoryPlugin:
        if not isinstance(config, Mem0MemoryPluginConfig):
            raise TypeError("mem0 memory plugin config is invalid")
        resolved = self._defaults.model_copy(
            update={
                name: getattr(config, name)
                for name in config.model_fields_set
            }
        )
        if context.provider_mode != "real":
            return Mem0MemoryPlugin(client=UnavailableMem0Client())
        base_url = _config_text(resolved.base_url)
        if base_url is None or not base_url.strip():
            return Mem0MemoryPlugin(client=UnavailableMem0Client())
        return Mem0MemoryPlugin(
            client=Mem0Client(
                base_url=base_url,
                api_key=(
                    resolved.api_key.get_secret_value()
                    if resolved.api_key is not None
                    else None
                ),
                timeout_seconds=resolved.timeout_seconds,
            )
        )


def default_memory_plugin_factories(
    config: ProviderConfig | None = None,
) -> tuple[Mem0MemoryPluginFactory, ...]:
    """Return the explicit built-in inventory for the composition root."""

    resolved = config or ProviderConfig.from_env()
    defaults = Mem0MemoryPluginConfig(
        base_url=resolved.mem0_base_url,
        api_key=(
            SecretStr(resolved.mem0_api_key)
            if resolved.mem0_api_key is not None
            else None
        ),
        timeout_seconds=resolved.mem0_timeout_seconds,
    )
    return (Mem0MemoryPluginFactory(defaults=defaults),)


def _issue(code: str, *, recoverable: bool) -> MemoryPluginIssue:
    return MemoryPluginIssue(
        code=code,
        message=code,
        recoverable=recoverable,
    )


def _config_text(value: SecretStr | str | None) -> str | None:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


__assistant_memory_plugin_factory__ = Mem0MemoryPluginFactory()
