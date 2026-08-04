from assistant_agent.media.embedding.coordinator_store import SessionEmbeddingCoordinatorStore


class _Coordinator:
    def __init__(self, user_id: str, session_id: str) -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_store_reuses_session_and_clears_owned_coordinator() -> None:
    store = SessionEmbeddingCoordinatorStore(factory=_Coordinator)

    first = store.resolve("user-1", "session-1")

    assert store.resolve("user-1", "session-1") is first
    assert store.clear_session("user-1", "session-1") is True
    assert first.closed is True
    assert store.clear_session("user-1", "session-1") is False


def test_store_ttl_evicts_and_closes_expired_coordinator() -> None:
    now = [10.0]
    store = SessionEmbeddingCoordinatorStore(
        factory=_Coordinator, ttl_seconds=5.0, clock=lambda: now[0]
    )
    first = store.resolve("user-1", "session-1")
    now[0] = 16.0

    second = store.resolve("user-1", "session-1")

    assert second is not first
    assert first.closed is True


def test_clear_user_and_close_only_close_owned_entries() -> None:
    store = SessionEmbeddingCoordinatorStore(factory=_Coordinator)
    first = store.resolve("user-1", "session-1")
    second = store.resolve("user-1", "session-2")
    other = store.resolve("user-2", "session-1")

    assert store.clear_user("user-1") == 2
    assert first.closed is second.closed is True
    assert other.closed is False
    store.close()
    assert other.closed is True
