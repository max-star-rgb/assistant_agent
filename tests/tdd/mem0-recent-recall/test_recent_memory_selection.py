from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.ingestion_queue import MemoryIngestionQueue
from assistant_agent.memory.mem0.client import Mem0Client
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.service import LongTermMemoryService
from assistant_agent.memory.session_snapshot import SessionMemorySnapshotStore
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.state import AgentState
from docker.mem0.mem0_env import collect_all_memories


def _identity() -> RequestIdentity:
    return RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )


def test_mem0_client_requests_and_returns_all_long_term_memories() -> None:
    requests = []
    results = [
        {
            "id": f"memory-{index}",
            "memory": f"value-{index}",
            "created_at": "2026-08-04T00:00:00+00:00",
        }
        for index in range(7)
    ]

    def transport(request):
        requests.append(request)
        return {"results": results}

    client = Mem0Client(
        base_url="http://memory.invalid",
        identity_namespace="test",
        transport=transport,
    )

    memories = client.recall_long_term_memory(_identity())

    engine_identity = bind_mem0_identity(_identity(), namespace="test")
    assert requests[0].query == engine_identity.long_term_filters
    assert [memory.memory_id for memory in memories] == [
        f"memory-{index}" for index in range(7)
    ]


def test_collect_all_memories_expands_until_the_full_result_is_available() -> None:
    results = [{"id": f"memory-{index}"} for index in range(5)]
    requested_top_k: list[int] = []

    def fetch(top_k: int):
        requested_top_k.append(top_k)
        return results[:top_k]

    collected = collect_all_memories(fetch, initial_top_k=2)

    assert collected == results
    assert requested_top_k == [2, 4, 8]


def test_memory_service_freezes_every_item_without_a_top_k_argument() -> None:
    class Client:
        configured = True

        def recall_long_term_memory(self, identity):
            assert identity == _identity()
            return [
                {
                    "memory_id": f"memory-{index}",
                    "text": f"value-{index}",
                    "created_at": "2026-08-04T00:00:00+00:00",
                }
                for index in range(7)
            ]

    client = Client()
    service = LongTermMemoryService(
        client=client,
        snapshot_store=SessionMemorySnapshotStore(),
        ingestion_queue=MemoryIngestionQueue(),
    )
    state = AgentState.from_request(
        UserRequest(
            user_id="user-sentinel",
            session_id="session-sentinel",
            text="request-sentinel",
        ),
        agent_id="agent-sentinel",
    )
    try:
        snapshot = service.initialize_session(
            identity=_identity(),
            state=state,
            trace_store=None,
        )
    finally:
        service.close(timeout=1.0)

    assert len(snapshot.memories) == 7
