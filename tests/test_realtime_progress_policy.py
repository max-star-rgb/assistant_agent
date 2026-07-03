from assistant_agent.realtime import ProgressPolicy, RealtimeAgentEvent


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _progress_event(*, progress: float | None = None) -> RealtimeAgentEvent:
    payload = {
        "stage": "tool",
        "status": "working",
        "current_step": "image_generation",
        "message": "Calling image_generation.",
    }
    if progress is not None:
        payload["progress"] = progress
    return RealtimeAgentEvent(
        type="run.progress",
        text="Calling image_generation.",
        payload=payload,
        display_only=True,
    )


def test_progress_policy_throttles_duplicate_progress_updates() -> None:
    clock = Clock()
    tracker = ProgressPolicy(min_interval_s=5.0).tracker(now_fn=clock)

    assert tracker.should_emit(_progress_event()) is True
    clock.advance(1.0)

    assert tracker.should_emit(_progress_event()) is False


def test_progress_policy_allows_significant_progress_delta() -> None:
    clock = Clock()
    tracker = ProgressPolicy(
        min_interval_s=5.0,
        significant_progress_delta=0.1,
    ).tracker(now_fn=clock)

    assert tracker.should_emit(_progress_event(progress=0.1)) is True
    clock.advance(1.0)

    assert tracker.should_emit(_progress_event(progress=0.25)) is True


def test_progress_policy_emits_heartbeat_after_idle_interval() -> None:
    clock = Clock()
    tracker = ProgressPolicy(
        min_interval_s=5.0,
        heartbeat_interval_s=10.0,
    ).tracker(now_fn=clock)

    assert tracker.should_emit(_progress_event()) is True
    clock.advance(9.0)
    assert tracker.heartbeat() is None
    clock.advance(1.0)

    heartbeat = tracker.heartbeat()

    assert heartbeat is not None
    assert heartbeat.type == "run.progress"
    assert heartbeat.display_only is True
    assert heartbeat.payload["heartbeat"] is True
    assert heartbeat.payload["current_step"] == "image_generation"
    assert heartbeat.payload["elapsed_since_update_s"] == 10.0
    assert heartbeat.text == "Still working on image_generation."
