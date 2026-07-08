from assistant_agent.realtime.audio_edge import gateway_frame_to_tts_event


def test_audio_edge_maps_gateway_response_chunk_to_tts_speak_event() -> None:
    event = gateway_frame_to_tts_event(
        {
            "type": "stream.chunk",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "run_id": "run-1",
            "payload": {
                "text": "这是最终回答的一部分。",
                "content_type": "text",
                "display_only": False,
            },
        }
    )

    assert event == {
        "type": "tts.speak",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "run_id": "run-1",
        "payload": {
            "text": "这是最终回答的一部分。",
            "source_frame": "stream.chunk",
            "content_type": "text",
            "display_only": False,
            "replaceable": False,
        },
    }


def test_audio_edge_maps_gateway_progress_to_replaceable_tts_progress_event() -> None:
    event = gateway_frame_to_tts_event(
        {
            "type": "event.progress",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "run_id": "run-1",
            "payload": {
                "text": "我先查一下。",
                "content_type": "text",
                "display_only": True,
                "replaceable": True,
            },
        }
    )

    assert event == {
        "type": "tts.progress",
        "session_id": "session-1",
        "turn_id": "turn-1",
        "run_id": "run-1",
        "payload": {
            "text": "我先查一下。",
            "source_frame": "event.progress",
            "content_type": "text",
            "display_only": True,
            "replaceable": True,
        },
    }


def test_audio_edge_ignores_non_text_and_control_frames() -> None:
    assert (
        gateway_frame_to_tts_event(
            {
                "type": "stream.chunk",
                "payload": {"text": "binary", "content_type": "audio/wav"},
            }
        )
        is None
    )
    assert gateway_frame_to_tts_event({"type": "run.end", "payload": {"text": "done"}}) is None


def test_audio_edge_tts_event_contains_only_prompt_safe_text_metadata() -> None:
    event = gateway_frame_to_tts_event(
        {
            "type": "stream.chunk",
            "session_id": "session-1",
            "payload": {
                "text": "请朗读这段文本。",
                "content_type": "text",
                "audio": "data:audio/wav;base64,AAAA",
                "raw_audio": b"audio-bytes",
                "provider_response": {"token": "sk-secret"},
            },
        }
    )

    assert event is not None
    dumped = str(event)
    assert event["payload"] == {
        "text": "请朗读这段文本。",
        "source_frame": "stream.chunk",
        "content_type": "text",
        "display_only": False,
        "replaceable": False,
    }
    assert "audio" not in dumped
    assert "base64" not in dumped
    assert "provider_response" not in dumped
    assert "sk-secret" not in dumped
