"""LangGraph checkpointer factory and process-owned async lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.graph_invocation_claims import (
    GraphInvocationClaimStore,
    InMemoryGraphInvocationClaimStore,
    SQLiteGraphInvocationClaimStore,
)


class CheckpointerConfigurationError(RuntimeError):
    """Fail-closed checkpointer composition error with a stable safe code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def create_checkpointer(config: ProviderConfig | None = None) -> Any | None:
    """Create only a nonpersistent saver outside an async process owner.

    SQLite connections are event-loop resources and therefore cannot be
    constructed by this compatibility factory.
    """

    resolved_config = config or ProviderConfig.from_env({})
    backend = resolved_config.langgraph_checkpointer_backend
    if backend == "sqlite":
        raise CheckpointerConfigurationError(
            "langgraph_checkpointer_owner_required",
            "SQLite LangGraph persistence requires AsyncCheckpointerOwner.",
        )
    return create_nonpersistent_checkpointer(resolved_config)


def create_nonpersistent_checkpointer(config: ProviderConfig) -> Any | None:
    """Create the explicit none or memory backend without durability claims."""

    backend = config.langgraph_checkpointer_backend
    if backend == "none":
        return None
    if backend == "memory":
        return MemorySaver()
    raise CheckpointerConfigurationError(
        "langgraph_checkpointer_owner_required",
        "Persistent LangGraph backends require AsyncCheckpointerOwner.",
    )


class AsyncCheckpointerOwner:
    """Own exactly one saver and its separate invocation-claim resource."""

    def __init__(self, config: ProviderConfig) -> None:
        if not isinstance(config, ProviderConfig):
            raise TypeError("config must be a ProviderConfig")
        self.config = config
        self._state: Literal["new", "open", "closed"] = "new"
        self._checkpointer: Any | None = None
        self._claim_store: GraphInvocationClaimStore | None = None
        self._saver_context: Any | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._checkpoint_path: Path | None = None
        self._claim_path: Path | None = None
        self._persistent = False

    @property
    def checkpointer(self) -> Any | None:
        self._require_open()
        return self._checkpointer

    @property
    def invocation_claim_store(self) -> GraphInvocationClaimStore:
        self._require_open()
        assert self._claim_store is not None
        return self._claim_store

    @property
    def is_persistent(self) -> bool:
        self._require_open()
        return self._persistent

    @property
    def checkpoint_path(self) -> Path | None:
        self._require_open()
        return self._checkpoint_path

    @property
    def claim_path(self) -> Path | None:
        self._require_open()
        return self._claim_path

    async def open(self) -> AsyncCheckpointerOwner:
        """Open the configured resources once and set up the official saver."""

        async with self._lifecycle_lock:
            if self._state != "new":
                raise CheckpointerConfigurationError(
                    "langgraph_checkpointer_owner_lifecycle",
                    "AsyncCheckpointerOwner can be opened exactly once.",
                )
            backend = self.config.langgraph_checkpointer_backend
            if backend in {"none", "memory"}:
                self._checkpointer = create_nonpersistent_checkpointer(self.config)
                self._claim_store = InMemoryGraphInvocationClaimStore()
                self._state = "open"
                return self
            if backend != "sqlite":
                raise CheckpointerConfigurationError(
                    "langgraph_checkpointer_backend_invalid",
                    "Unsupported LangGraph checkpointer backend.",
                )

            raw_path = self.config.langgraph_checkpoint_path
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise CheckpointerConfigurationError(
                    "langgraph_checkpoint_path_required",
                    "SQLite LangGraph persistence requires LANGGRAPH_CHECKPOINT_PATH.",
                )
            checkpoint_path = Path(raw_path)
            if not checkpoint_path.is_absolute():
                raise CheckpointerConfigurationError(
                    "langgraph_checkpoint_path_invalid",
                    "LANGGRAPH_CHECKPOINT_PATH must be an absolute deployment path.",
                )
            if checkpoint_path.exists() and not checkpoint_path.is_file():
                raise CheckpointerConfigurationError(
                    "langgraph_checkpoint_path_invalid",
                    "LANGGRAPH_CHECKPOINT_PATH must refer to a file.",
                )
            try:
                saver_type = _load_async_sqlite_saver()
            except (ImportError, ModuleNotFoundError) as exc:
                raise CheckpointerConfigurationError(
                    "langgraph_sqlite_dependency_unavailable",
                    "The official async SQLite LangGraph saver is unavailable.",
                ) from exc

            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            claim_path = checkpoint_path.with_name(
                f"{checkpoint_path.name}.claims.sqlite3"
            )
            saver_context = saver_type.from_conn_string(str(checkpoint_path))
            try:
                saver = await saver_context.__aenter__()
                try:
                    await saver.setup()
                    claim_store = SQLiteGraphInvocationClaimStore(claim_path)
                except BaseException:
                    await saver_context.__aexit__(None, None, None)
                    raise
            except CheckpointerConfigurationError:
                raise
            except BaseException as exc:
                raise CheckpointerConfigurationError(
                    "langgraph_sqlite_open_failed",
                    "The persistent LangGraph resources could not be opened.",
                ) from exc

            self._checkpointer = saver
            self._claim_store = claim_store
            self._saver_context = saver_context
            self._checkpoint_path = checkpoint_path
            self._claim_path = claim_path
            self._persistent = True
            self._state = "open"
            return self

    async def aclose(self) -> None:
        """Close claims before the saver and make the owner unusable."""

        async with self._lifecycle_lock:
            if self._state == "closed":
                return
            if self._state != "open":
                self._state = "closed"
                return
            self._state = "closed"
            claim_store = self._claim_store
            saver_context = self._saver_context
            self._claim_store = None
            self._checkpointer = None
            self._saver_context = None
            first_error: BaseException | None = None
            close_claims = getattr(claim_store, "close", None)
            if callable(close_claims):
                try:
                    close_claims()
                except BaseException as exc:  # pragma: no cover - defensive cleanup
                    first_error = exc
            if saver_context is not None:
                try:
                    await saver_context.__aexit__(None, None, None)
                except BaseException as exc:  # pragma: no cover - defensive cleanup
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

    async def __aenter__(self) -> AsyncCheckpointerOwner:
        return await self.open()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    def _require_open(self) -> None:
        if self._state != "open":
            raise CheckpointerConfigurationError(
                "langgraph_checkpointer_owner_not_open",
                "AsyncCheckpointerOwner is not open.",
            )


def open_checkpointer(config: ProviderConfig) -> AsyncCheckpointerOwner:
    """Return the async process owner used by composition roots."""

    return AsyncCheckpointerOwner(config)


def _load_async_sqlite_saver() -> Any:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    return AsyncSqliteSaver


__all__ = [
    "AsyncCheckpointerOwner",
    "CheckpointerConfigurationError",
    "create_checkpointer",
    "create_nonpersistent_checkpointer",
    "open_checkpointer",
]
