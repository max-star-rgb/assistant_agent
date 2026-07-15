from assistant_agent.realtime.cancellation import (
    build_realtime_turn_cancellation_metadata,
    realtime_turn_cancellation_from_metadata,
)


def test_realtime_turn_cancellation_contract_maps_gateway_interrupt() -> None:
    metadata = build_realtime_turn_cancellation_metadata(
        {"cancel_source": "gateway_interrupt"},
        phase="tool_running",
    )

    assert metadata["cancel_source"] == "gateway_interrupt"
    assert metadata["stale_outputs"] is True
    assert metadata["can_reuse_tool_result"] is False
    assert metadata["speakable"] is False
    assert metadata["realtime_turn_cancellation"] == {
        "cancelled_by": "interrupt",
        "phase": "tool_running",
        "stale_outputs": True,
        "can_reuse_tool_result": False,
        "speakable": False,
    }


def test_realtime_turn_cancellation_contract_preserves_existing_payload() -> None:
    metadata = build_realtime_turn_cancellation_metadata(
        {
            "cancel_source": "deadline",
            "cancel_reason": "run_deadline_expired",
            "deadline_ms": 50,
        },
        phase="before_llm",
    )

    contract = realtime_turn_cancellation_from_metadata(metadata)

    assert contract.cancelled_by == "deadline"
    assert contract.phase == "before_llm"
    assert contract.stale_outputs is True
    assert contract.can_reuse_tool_result is False
    assert contract.speakable is False
    assert metadata["cancel_reason"] == "run_deadline_expired"
    assert metadata["deadline_ms"] == 50
