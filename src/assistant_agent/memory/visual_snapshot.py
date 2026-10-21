"""User-scoped visual background snapshots for the native voice frontend."""

import asyncio
from dataclasses import dataclass, field
import logging

from assistant_agent.memory.remote_service import RemoteMemoryServiceClient


SNAPSHOT_QUERY = "用户近期看到的场景、物品及其位置、活动和重要事件"
logger = logging.getLogger(__name__)


@dataclass
class _Snapshot:
    texts: list[str] = field(default_factory=list)
    completed_files: dict[str, None] = field(default_factory=dict)
    count: int = 0
    status: str = "empty"
    dirty: bool = False
    task: asyncio.Task | None = None


class VisualMemorySnapshots:
    def __init__(self, *, client: RemoteMemoryServiceClient, top_k: int = 8):
        self.client = client
        self.top_k = min(top_k, 32)
        self._users: dict[str, _Snapshot] = {}
        self._closed = False

    def read(self, user_id: str) -> dict:
        state = self._users.get(user_id)
        return {"enabled": True, "texts": list(state.texts) if state else [],
                "completed_segments": state.count if state else 0,
                "status": state.status if state else "empty"}

    def completed(self, user_id: str, file_id: str) -> None:
        if self._closed:
            return
        state = self._users.setdefault(user_id, _Snapshot())
        if file_id in state.completed_files:
            return
        state.completed_files[file_id] = None
        # Bound process-local retry deduplication; restart begins a new cadence.
        if len(state.completed_files) > 4096:
            del state.completed_files[next(iter(state.completed_files))]
        state.count += 1
        if state.count % 10 == 0:
            self.refresh(user_id)

    def refresh(self, user_id: str) -> None:
        if self._closed:
            return
        state = self._users.setdefault(user_id, _Snapshot())
        state.dirty = True
        if state.task is None or state.task.done():
            state.task = asyncio.create_task(self._refresh(user_id, state))

    async def initialize(self, user_id: str) -> None:
        self.refresh(user_id)
        if not self._closed:
            await asyncio.shield(self._users[user_id].task)

    async def _refresh(self, user_id: str, state: _Snapshot) -> None:
        while state.dirty and not self._closed:
            state.dirty = False
            state.status = "refreshing"
            try:
                texts = await self.client.query_memories(
                    user_id=user_id, query=SNAPSHOT_QUERY, top_k=self.top_k)
                bounded, remaining = [], 12000
                for text in texts[:self.top_k]:
                    text = text[:min(4000, remaining)]
                    if text:
                        bounded.append(text)
                        remaining -= len(text)
                if not self._closed:
                    state.texts, state.status = bounded, "ready"
            except Exception as exc:  # Background recall must not interrupt voice.
                state.status = "unavailable"
                logger.warning("visual_snapshot_failed error_type=%s", type(exc).__name__)

    async def wait(self) -> None:
        await asyncio.gather(*(s.task for s in self._users.values() if s.task))

    async def aclose(self) -> None:
        self._closed = True
        tasks = [s.task for s in self._users.values() if s.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._users.clear()
