from __future__ import annotations

from pathlib import Path
import json

from assistant_agent.context.builder import _trim_observations_to_chars
from assistant_agent.context.compaction import project_observations_for_context
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.embedding.observability import InMemoryEmbeddingObserver
from assistant_agent.context.token_budget import ContextWindowPolicy
from assistant_agent.media.video.visual_timeline_context import (
    VisualTimelineCompaction,
    VisualTimelineCompactionError,
    VisualTimelineContextService,
)
from assistant_agent.tools.base import ToolContext
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    VisualMemorySearchInput,
    VisualMemorySearchTool,
    VisualMemoryTimeWindow,
)


def _record(
    store: SessionVisualSemanticStore,
    evidence: Path,
    *,
    sequence: int,
    summary: str | None = None,
) -> None:
    store.record_success(
        VisualSemanticRecord(
            record_id=f"record-{sequence}",
            session_id="session-1",
            video_id="video-1",
            frame_sequence=sequence,
            captured_at_ms=sequence * 1_000,
            summary=summary or f"frame-{sequence}-text",
            index_status="unavailable",
            evidence_ref=str(evidence),
            evidence_bytes=evidence.stat().st_size,
            created_at_ms=sequence * 1_000 + 10,
        )
    )


def _visual_memory_observation(count: int = 256) -> dict[str, object]:
    return {
        "tool_name": "visual_memory_search",
        "status": "succeeded",
        "summary": f"retained {count} visual observations",
        "outcome": "success",
        "is_complete": True,
        "data": {
            "status": "records",
            "observations": [
                {"timestamp_ms": sequence * 1_000, "text": f"frame-{sequence}-text"}
                for sequence in range(1, count + 1)
            ],
            "observation_count": count,
            "errors": [],
        },
    }


class _ToolProjectionCounter:
    tokenizer_id = "tool-projection-counter"

    def count_text(self, value: str) -> int:
        payload = json.loads(value)
        observations = payload.get("observations")
        summary = payload.get("timeline_summary")
        return (
            len(observations) * 100 if isinstance(observations, list) else 0
        ) + (len(summary) if isinstance(summary, str) else 0)


class _ToolTailCompactor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def compact(self, **_kwargs) -> VisualTimelineCompaction:
        if self.fail:
            raise VisualTimelineCompactionError("provider_unavailable")
        return VisualTimelineCompaction(
            summary="旧画面中曾出现黑色手机。",
            relevant_observation_indexes=[2],
        )


def _timeline_context_service(*, fail: bool = False) -> VisualTimelineContextService:
    return VisualTimelineContextService(
        compactor=_ToolTailCompactor(fail=fail),
        token_counter=_ToolProjectionCounter(),
        window_policy=ContextWindowPolicy(
            input_token_limit=2_000,
            target_ratio=0.40,
            trigger_ratio=0.60,
            hard_ratio=0.90,
            summary_max_tokens=500,
        ),
        keep_recent_observations=2,
    )


def test_store_text_timeline_returns_all_256_records_at_as_of_boundary(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-1",
        max_records=300,
    )
    for sequence in range(1, 258):
        _record(store, evidence, sequence=sequence)

    records = store.text_timeline(as_of_sequence=256)

    assert len(records) == 256
    assert [record.frame_sequence for record in records] == list(range(1, 257))
    assert records[-1] is not store.at_or_before("video-1", sequence=256)
    assert store.has_visual_history() is True


def test_store_text_timeline_applies_timestamp_window(tmp_path: Path) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-1",
    )
    for sequence in range(1, 6):
        _record(store, evidence, sequence=sequence)

    records = store.text_timeline(
        as_of_sequence=5,
        since_ms=2_000,
        until_ms=4_000,
    )

    assert [record.frame_sequence for record in records] == [2, 3, 4]


def test_visual_memory_tool_returns_all_256_vlm_text_observations(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    store = pool.resolve("user-1", "session-1")
    for sequence in range(1, 257):
        _record(
            store,
            evidence,
            sequence=sequence,
            summary=(
                "白色桌面上放着一部屏幕显示 3:25 的黑色智能手机。"
                if sequence == 33
                else f"frame-{sequence}-text"
            ),
        )
    tool = VisualMemorySearchTool(semantic_store_pool=pool)

    result = tool.run(
        VisualMemorySearchInput(query="黑色手机", session_id="session-1"),
        ToolContext(
            user_id="user-1",
            session_id="session-1",
            metadata={
                "request_metadata": {
                    "_trusted_visual_memory_as_of_sequence": 256,
                }
            },
        ),
    )

    assert result.success is True
    assert result.model_observation["status"] == "records"
    assert result.model_observation["observation_count"] == 256
    assert len(result.model_observation["observations"]) == 256
    assert result.model_observation["observations"][32] == {
        "timestamp_ms": 33_000,
        "text": "白色桌面上放着一部屏幕显示 3:25 的黑色智能手机。",
    }
    assert "similarity" not in result.model_observation


def test_visual_memory_tool_applies_as_of_and_time_window_without_ranking(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    store = pool.resolve("user-1", "session-1")
    for sequence in range(1, 6):
        _record(store, evidence, sequence=sequence)
    tool = VisualMemorySearchTool(semantic_store_pool=pool)

    result = tool.run(
        VisualMemorySearchInput(
            query="does-not-filter",
            search_mode="event",
            time_window=VisualMemoryTimeWindow(start_ms=2_000, end_ms=4_000),
            session_id="session-1",
        ),
        ToolContext(
            user_id="user-1",
            session_id="session-1",
            metadata={
                "request_metadata": {
                    "_trusted_visual_memory_as_of_sequence": 3,
                }
            },
        ),
    )

    assert result.model_observation["observations"] == [
        {"timestamp_ms": 2_000, "text": "frame-2-text"},
        {"timestamp_ms": 3_000, "text": "frame-3-text"},
    ]


def test_context_projection_preserves_all_256_visual_memory_observations() -> None:
    projected = project_observations_for_context([_visual_memory_observation()])

    assert projected[0]["data"]["observation_count"] == 256
    assert len(projected[0]["data"]["observations"]) == 256
    assert projected[0]["data"]["observations"][-1] == {
        "timestamp_ms": 256_000,
        "text": "frame-256-text",
    }


def test_soft_context_budget_does_not_summarize_visual_memory_timeline() -> None:
    observation = project_observations_for_context([_visual_memory_observation()])[0]

    trimmed = _trim_observations_to_chars([observation], max_chars=100)

    assert trimmed == [observation]


def test_context_projection_keeps_generic_list_limit_for_other_tools() -> None:
    projected = project_observations_for_context(
        [
            {
                "tool_name": "some_other_tool",
                "status": "succeeded",
                "data": {"items": list(range(10))},
            }
        ]
    )

    assert projected[0]["data"]["items"] == [0, 1, 2]


def test_context_projection_preserves_compacted_visual_timeline_contract() -> None:
    observation = _visual_memory_observation(count=3)
    observation["data"].update(
        {
            "returned_observation_count": 3,
            "timeline_summary": "较早画面中曾出现黑色手机。",
            "coverage": {
                "source_count": 256,
                "covered_count": 253,
                "returned_count": 3,
                "start_ms": 1_000,
                "end_ms": 253_000,
                "digest": "0123456789abcdef",
            },
            "compaction": {
                "status": "succeeded",
                "tokenizer_id": "main-model-tokenizer",
                "input_tokens": 20_000,
                "output_tokens": 700,
                "effective_input_limit": 16_000,
                "target_tokens": 800,
                "triggered": True,
                "hard": True,
                "attempts": 1,
                "target_reached": True,
            },
        }
    )

    projected = project_observations_for_context([observation])

    assert projected[0]["data"] == observation["data"]


def test_visual_memory_tool_tail_compacts_before_model_observation(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    observer = InMemoryEmbeddingObserver()
    pool = SessionVisualSemanticStorePool(
        root=tmp_path / "pool",
        observer=observer,
    )
    store = pool.resolve("user-1", "session-1")
    for sequence in range(1, 21):
        _record(store, evidence, sequence=sequence)
    tool = VisualMemorySearchTool(
        semantic_store_pool=pool,
        timeline_context_service=_timeline_context_service(),
    )

    result = tool.run(
        VisualMemorySearchInput(query="黑色手机", session_id="session-1"),
        ToolContext(user_id="user-1", session_id="session-1"),
    )

    assert result.success is True
    assert result.model_observation["status"] == "records"
    assert result.model_observation["observation_count"] == 20
    assert result.model_observation["returned_observation_count"] == 3
    assert result.model_observation["observations"] == [
        {"timestamp_ms": 3_000, "text": "frame-3-text"},
        {"timestamp_ms": 19_000, "text": "frame-19-text"},
        {"timestamp_ms": 20_000, "text": "frame-20-text"},
    ]
    assert result.model_observation["timeline_summary"] == "旧画面中曾出现黑色手机。"
    assert result.model_observation["coverage"]["covered_count"] == 18
    assert result.model_observation["compaction"]["status"] == "succeeded"
    compaction_event = next(
        event
        for event in observer.events
        if event.event_name == "visual_memory.compaction"
    )
    assert compaction_event.payload["count"] == 20
    assert compaction_event.payload["returned_count"] == 3
    assert compaction_event.payload["status"] == "succeeded"
    assert "黑色手机" not in str(compaction_event.model_dump())


def test_visual_memory_tool_hard_compaction_failure_blocks_raw_timeline(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "frame.jpg"
    evidence.write_bytes(b"jpeg")
    pool = SessionVisualSemanticStorePool(root=tmp_path / "pool")
    store = pool.resolve("user-1", "session-1")
    for sequence in range(1, 21):
        _record(store, evidence, sequence=sequence)
    tool = VisualMemorySearchTool(
        semantic_store_pool=pool,
        timeline_context_service=_timeline_context_service(fail=True),
    )

    result = tool.run(
        VisualMemorySearchInput(query="黑色手机", session_id="session-1"),
        ToolContext(user_id="user-1", session_id="session-1"),
    )

    assert result.success is False
    assert result.model_observation == {
        "status": "unavailable",
        "observations": [],
        "observation_count": 20,
        "returned_observation_count": 0,
        "errors": [
            {
                "code": "visual_memory_context_hard_limit",
                "message": "visual timeline could not be compacted below the hard limit",
                "recoverable": True,
            }
        ],
    }
