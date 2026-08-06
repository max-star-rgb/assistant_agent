from __future__ import annotations

import pytest

from docker.mem0.mem0_env import clear_memories


class _MemoryStore:
    def __init__(self) -> None:
        self.records = [
            {"id": "memory-1", "user_id": "user-a", "agent_id": "agent-a"},
            {"id": "memory-2", "user_id": "user-a", "agent_id": "agent-b"},
            {"id": "memory-3", "user_id": "user-b", "agent_id": "agent-a"},
        ]
        self.events: list[tuple[str, object]] = []

    def _get_all_from_vector_store(
        self,
        filters: dict[str, str],
        top_k: int,
        show_expired: bool,
        output_limit: int,
    ) -> list[dict[str, str]]:
        assert show_expired is True
        matching = [
            record
            for record in self.records
            if all(record.get(key) == value for key, value in filters.items())
        ]
        return matching[: min(top_k, output_limit)]

    def delete_all(self, **filters: str) -> None:
        self.events.append(("delete_all", filters))
        self._delete_matching(filters)

    def _delete_matching(
        self,
        filters: dict[str, str],
        *,
        limit: int | None = None,
    ) -> None:
        matching_ids = [
            record["id"]
            for record in self.records
            if all(record.get(key) == value for key, value in filters.items())
        ][:limit]
        self.records = [
            record for record in self.records if record["id"] not in matching_ids
        ]

    def reset(self) -> None:
        self.events.append(("reset", None))


class _MemoryStoreWithPagedIdentityDelete(_MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.records = [
            {"id": f"memory-{index}", "user_id": "user-a"}
            for index in range(205)
        ]

    def delete_all(self, **filters: str) -> None:
        self.events.append(("delete_all", filters))
        self._delete_matching(filters, limit=100)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"all": False},
        {"all": "true"},
        {"all": True, "user_id": "user-a"},
    ],
)
def test_clear_rejects_ambiguous_scope_without_deleting(payload: dict) -> None:
    memory = _MemoryStore()

    with pytest.raises(ValueError):
        clear_memories(memory, payload)

    assert memory.events == []


def test_clear_identity_deletes_only_matching_scope_with_complete_count() -> None:
    memory = _MemoryStore()

    result = clear_memories(memory, {"user_id": "user-a"})

    assert result == {
        "success": True,
        "scope": "identity",
        "filters": {"user_id": "user-a"},
        "deleted_count": 2,
    }
    assert memory.events == [("delete_all", {"user_id": "user-a"})]


def test_clear_all_resets_store_without_using_identity_delete() -> None:
    memory = _MemoryStore()

    result = clear_memories(memory, {"all": True})

    assert result == {
        "success": True,
        "scope": "all",
        "deleted_count": 3,
    }
    assert memory.events == [("reset", None)]


def test_clear_identity_repeats_upstream_paged_delete_until_scope_is_empty() -> None:
    memory = _MemoryStoreWithPagedIdentityDelete()

    result = clear_memories(memory, {"user_id": "user-a"})

    assert result["deleted_count"] == 205
    assert memory.records == []
    assert memory.events == [
        ("delete_all", {"user_id": "user-a"}),
        ("delete_all", {"user_id": "user-a"}),
        ("delete_all", {"user_id": "user-a"}),
    ]
