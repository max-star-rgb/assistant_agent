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


def test_first_progress_timeout_can_disable_sla_fallback() -> None:
    clock = Clock()

    disabled = ProgressPolicy(first_progress_timeout_s=0).tracker(now_fn=clock)
    negative = ProgressPolicy(first_progress_timeout_s=-1).tracker(now_fn=clock)

    assert disabled.first_progress_fallback() is None
    assert negative.first_progress_fallback() is None


def test_progress_tracker_records_user_visible_events() -> None:
    clock = Clock()
    tracker = ProgressPolicy().tracker(now_fn=clock)

    assert tracker.has_user_visible_event is False

    clock.advance(0.25)
    assert tracker.should_emit(
        RealtimeAgentEvent(
            type="response.chunk",
            text="partial",
            payload={"source": "stream"},
        )
    )

    assert tracker.has_user_visible_event is True
    assert tracker.summary() == {
        "first_visible_event_ms": 250.0,
        "sla_fallback_emitted": False,
        "user_visible_event_count": 1,
    }


def test_first_progress_fallback_payload_is_display_only_and_replaceable() -> None:
    tracker = ProgressPolicy(
        first_progress_timeout_s=0.8,
        first_progress_message="I am on it.",
    ).tracker()

    fallback = tracker.first_progress_fallback()

    assert fallback is not None
    assert fallback.type == "run.progress"
    assert fallback.text == "I am on it."
    assert fallback.display_only is True
    assert fallback.payload == {
        "source": "realtime_sla_fallback",
        "replaceable": True,
        "display_only": True,
        "stage": "runtime",
        "status": "working",
        "current_step": "awaiting_first_output",
        "fallback_policy_version": "v1",
        "message": "I am on it.",
    }
