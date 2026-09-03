from assistant_agent.agent_server.media_app import _NativeAssistantTextStream


def test_public_response_uses_native_message_stream() -> None:
    stream = _NativeAssistantTextStream()
    message_id = "native-response-message"
    stream.consume(
        {
            "event": "messages/metadata",
            "data": {
                message_id: {
                    "metadata": {
                        "langgraph_node": "model",
                        "langgraph_checkpoint_ns": "assistant_agent:sentinel",
                    }
                }
            },
        }
    )

    assert stream.consume(
        {
            "event": "messages/partial",
            "data": [
                {
                    "id": message_id,
                    "type": "AIMessageChunk",
                    "content": "response-sentinel",
                }
            ],
        }
    ) == [(1, "response-sentinel")]
