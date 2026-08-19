from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from assistant_agent.media.video.realtime_video_memory import RealtimeVideoMemoryStore
from assistant_agent.media.video.semantic_store import (
    SessionVisualSemanticStore,
    VisualSemanticRecord,
)
from assistant_agent.media.vision.models import VideoUnderstandingRequest
from assistant_agent.tools.plugins.builtin.media_inspection import video_branch
from assistant_agent.tools.plugins.builtin.media_inspection.video_branch import (
    VideoUnderstandingBranch,
)
from assistant_agent.tools.runtime import ToolContext


class SignallingSemanticStore(SessionVisualSemanticStore):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.wait_started = Event()

    def wait_for_sequence(self, *args, **kwargs):
        self.wait_started.set()
        return super().wait_for_sequence(*args, **kwargs)


class StaticSemanticStorePool:
    def __init__(self, store: SessionVisualSemanticStore) -> None:
        self.store = store

    def peek(self, _user_id: str, _session_id: str) -> SessionVisualSemanticStore:
        return self.store


def _record(
    tmp_path: Path,
    *,
    sequence: int,
    video_id: str = "video-window",
) -> VisualSemanticRecord:
    evidence = tmp_path / f"evidence-{sequence}.jpg"
    evidence.write_bytes(f"frame-{sequence}".encode())
    return VisualSemanticRecord(
        record_id=f"record-{sequence}",
        session_id="session-window",
        video_id=video_id,
        frame_sequence=sequence,
        captured_at_ms=sequence * 100,
        summary=f"sequence-{sequence}",
        index_status="unavailable",
        evidence_ref=str(evidence),
        evidence_bytes=evidence.stat().st_size,
        created_at_ms=sequence * 100 + 1,
    )


def _context() -> ToolContext:
    return ToolContext(
        user_id="user-window",
        session_id="session-window",
        metadata={
            "entry_profile": "agent_service",
            "visual_window_id": "visual-window-test",
            "visual_window_start_sequence": 4,
            "visual_target_sequence": 8,
        },
    )


def _branch(store: SessionVisualSemanticStore) -> VideoUnderstandingBranch:
    return VideoUnderstandingBranch(
        semantic_store_pool=StaticSemanticStorePool(store),
        memory_store=RealtimeVideoMemoryStore(),
    )


def test_target_eight_releases_without_waiting_for_seven(tmp_path: Path) -> None:
    """Regression: a pending context frame must not extend the target barrier."""

    store = SignallingSemanticStore(
        root=tmp_path / "store",
        session_id="session-window",
    )
    for sequence in (3, 4, 5, 6, 9):
        store.record_success(_record(tmp_path, sequence=sequence))
    branch = _branch(store)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            branch.execute,
            VideoUnderstandingRequest(video_ref="video-window"),
            _context(),
        )
        assert store.wait_started.wait(timeout=1) is True
        assert future.done() is False

        store.record_success(_record(tmp_path, sequence=8))
        result = future.result(timeout=1)

    assert result.success is True
    assert result.data["window_start_sequence"] == 4
    assert result.data["target_sequence"] == 8
    assert result.data["ready_sequences"] == [4, 5, 6, 8]
    assert result.data["missing_sequences"] == [7]
    assert result.data["target_ready"] is True
    assert [item["sequence"] for item in result.data["observations"]] == [4, 5, 6, 8]
    assert result.data["observations"][-1]["role"] == "target"
    assert all(item["sequence"] not in {3, 9} for item in result.data["observations"])


@pytest.mark.parametrize("terminal", ["failed", "timeout"])
def test_target_failure_or_timeout_never_uses_sequence_seven_as_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
) -> None:
    """Regression: an older success must not masquerade as the exact target."""

    store = SessionVisualSemanticStore(
        root=tmp_path / "store",
        session_id="session-window",
    )
    store.record_success(_record(tmp_path, sequence=7))
    if terminal == "failed":
        store.record_failure(
            "video-window",
            sequence=8,
            error={"code": "target-failed", "message": "target failed"},
        )
    else:
        monkeypatch.setattr(video_branch, "LIVE_VIEW_SNAPSHOT_WAIT_SECONDS", 0.01)

    result = _branch(store).execute(
        VideoUnderstandingRequest(video_ref="video-window"),
        _context(),
    )

    assert result.success is True
    assert result.data["usable_visual_text"] is False
    assert result.data["target_ready"] is False
    assert result.data["target_status"] == terminal
    assert result.data["target_sequence"] == 8
    assert "sequence-7" not in str(result.model_observation)

