from __future__ import annotations

import pytest

from assistant_agent.config import ProviderConfig
from assistant_agent.media.embedding.models import EmbeddingEvent
from assistant_agent.media.video.visual_reminder import (
    VisualReminderClosedError,
    VisualReminderManager,
    VisualReminderRegistry,
)


def _event(
    observation_id: str,
    modality: str,
    vector: list[float],
    *,
    space: str = "joint-v1",
    normalized: bool = True,
) -> EmbeddingEvent:
    return EmbeddingEvent(
        event_id=f"event-{observation_id}",
        modality=modality,
        vector=vector,
        embedding_space_id=space,
        model_id="siglip2-test",
        model_revision="rev-test",
        dimension=len(vector),
        normalized=normalized,
        session_id="s1",
        source_observation_id=observation_id,
        latency_ms=1,
    )


def _manager(**overrides) -> VisualReminderManager:
    return VisualReminderManager(
        user_id="u1",
        session_id="s1",
        similarity_threshold=overrides.pop("similarity_threshold", 0.82),
        max_active=overrides.pop("max_active", 16),
        terminal_history_limit=overrides.pop("terminal_history_limit", 64),
        **overrides,
    )


def test_manager_supports_multiple_deduplicated_one_shot_reminders() -> None:
    manager = _manager()
    first = manager.create(
        target="水已经烧开",
        message="水烧开了",
        target_embedding=_event("target-1", "text", [1.0, 0.0]),
    )
    duplicate = manager.create(
        target=" 水已经烧开 ",
        message="水烧开了",
        target_embedding=_event("target-2", "text", [1.0, 0.0]),
    )
    second = manager.create(
        target="有人进门",
        message="有人进门了",
        target_embedding=_event("target-3", "text", [0.0, 1.0]),
    )

    assert duplicate.reminder_id == first.reminder_id
    assert len(manager.list_records()) == 2

    reserved = manager.reserve_matches(_event("frame-1", "image", [0.9, 0.1]))

    assert [item.reminder_id for item in reserved] == [first.reminder_id]
    result = manager.confirm(
        first.reminder_id,
        reservation_id=reserved[0].reservation_id,
    )
    assert result.status == "triggered"
    assert manager.reserve_matches(_event("frame-2", "image", [1.0, 0.0])) == []
    assert manager.list_records()[1].reminder_id == second.reminder_id
    assert manager.list_records()[1].status == "pending"


def test_cancel_and_release_require_current_pending_or_reservation_state() -> None:
    manager = _manager()
    record = manager.create(
        target="水已经烧开",
        message="水烧开了",
        target_embedding=_event("target", "text", [1.0, 0.0]),
    )
    reservation = manager.reserve_matches(_event("frame", "image", [1.0, 0.0]))[0]

    cancelled = manager.cancel(record.reminder_id)
    wrong_release = manager.release(record.reminder_id, reservation_id="wrong")
    released = manager.release(
        record.reminder_id,
        reservation_id=reservation.reservation_id,
    )
    cancelled_after_release = manager.cancel(record.reminder_id)

    assert (cancelled.changed, cancelled.status) == (False, "reserved")
    assert (wrong_release.changed, wrong_release.status) == (False, "reserved")
    assert (released.changed, released.status) == (True, "pending")
    assert (cancelled_after_release.changed, cancelled_after_release.status) == (
        True,
        "cancelled",
    )


def test_manager_enforces_active_limit_and_bounds_terminal_history() -> None:
    manager = _manager(max_active=1, terminal_history_limit=1)
    first = manager.create(
        target="one",
        message="one",
        target_embedding=_event("target-1", "text", [1.0, 0.0]),
    )
    with pytest.raises(ValueError, match="visual_reminder_active_limit"):
        manager.create(
            target="two",
            message="two",
            target_embedding=_event("target-2", "text", [0.0, 1.0]),
        )
    assert manager.cancel(first.reminder_id).changed is True

    second = manager.create(
        target="two",
        message="two",
        target_embedding=_event("target-2", "text", [0.0, 1.0]),
    )
    assert manager.cancel(second.reminder_id).changed is True

    records = manager.list_records()
    assert [(record.target, record.status) for record in records] == [
        ("two", "cancelled")
    ]


def test_incompatible_target_isolated_from_other_reminders() -> None:
    manager = _manager()
    manager.create(
        target="wrong-space",
        message="wrong-space",
        target_embedding=_event("wrong", "text", [1.0, 0.0], space="other"),
    )
    compatible = manager.create(
        target="match",
        message="match",
        target_embedding=_event("right", "text", [1.0, 0.0]),
    )

    matches = manager.reserve_matches(_event("frame", "image", [1.0, 0.0]))

    assert [match.reminder_id for match in matches] == [compatible.reminder_id]


@pytest.mark.parametrize(
    "event",
    [
        _event("not-normalized", "text", [1.0, 0.0], normalized=False),
        _event("non-finite", "text", [float("nan"), 0.0]),
        _event("zero", "text", [0.0, 0.0]),
    ],
)
def test_manager_rejects_unusable_target_embedding(event: EmbeddingEvent) -> None:
    with pytest.raises(ValueError, match="visual reminder target embedding is unusable"):
        _manager().create(target="target", message="message", target_embedding=event)


def test_close_rejects_new_records_and_registry_unregister_is_identity_safe() -> None:
    registry = VisualReminderRegistry()
    manager = _manager()
    replacement = _manager()
    registry.register(manager)
    registry.register(replacement)

    assert registry.unregister("u1", "s1", manager=manager) is False
    assert registry.peek("u1", "s1") is replacement
    assert registry.unregister("u1", "s1", manager=replacement) is True

    manager.close()
    assert manager.list_records() == []
    with pytest.raises(VisualReminderClosedError):
        manager.create(
            target="x",
            message="y",
            target_embedding=_event("target", "text", [1.0, 0.0]),
        )


def test_visual_reminder_configuration_is_loaded_and_validated() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
            "REALTIME_VISUAL_REMINDER_SIMILARITY_THRESHOLD": "0.75",
            "REALTIME_VISUAL_REMINDER_MAX_ACTIVE": "4",
            "REALTIME_VISUAL_REMINDER_TERMINAL_HISTORY_LIMIT": "8",
        }
    )

    assert config.visual_reminder_similarity_threshold == 0.75
    assert config.visual_reminder_max_active == 4
    assert config.visual_reminder_terminal_history_limit == 8

    for name, value in (
        ("REALTIME_VISUAL_REMINDER_SIMILARITY_THRESHOLD", "1.1"),
        ("REALTIME_VISUAL_REMINDER_MAX_ACTIVE", "0"),
        ("REALTIME_VISUAL_REMINDER_TERMINAL_HISTORY_LIMIT", "0"),
    ):
        with pytest.raises(ValueError):
            ProviderConfig.from_env(
                {
                    "MULTIMODAL_AGENT_PROVIDER_MODE": "mock",
                    name: value,
                }
            )
