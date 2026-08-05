from __future__ import annotations

from docker.mem0.mem0_env import list_unfiltered_memories


class _MemoryWithRawVectorListing:
    def __init__(self, results: list[dict[str, str]]) -> None:
        self.results = results
        self.calls: list[tuple[dict[str, str], int, bool, int]] = []

    def _get_all_from_vector_store(
        self,
        filters: dict[str, str],
        top_k: int,
        show_expired: bool,
        output_limit: int,
    ) -> list[dict[str, str]]:
        self.calls.append((filters, top_k, show_expired, output_limit))
        return self.results[:top_k]


def test_unfiltered_listing_expands_without_calling_identity_scoped_get_all() -> None:
    results = [{"id": f"memory-{index}"} for index in range(101)]
    memory = _MemoryWithRawVectorListing(results)

    listed = list_unfiltered_memories(memory)

    assert listed == results
    assert memory.calls == [({}, 100, True, 100), ({}, 200, True, 200)]


def test_unfiltered_listing_honors_operator_limit() -> None:
    results = [{"id": f"memory-{index}"} for index in range(3)]
    memory = _MemoryWithRawVectorListing(results)

    listed = list_unfiltered_memories(memory, limit=2)

    assert listed == results[:2]
    assert memory.calls == [({}, 2, True, 2)]
