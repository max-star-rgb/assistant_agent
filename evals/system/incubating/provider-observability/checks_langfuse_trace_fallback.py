"""开发期 Langfuse trace fallback 映射检查。"""

import json

from scripts import agentruntime_view


class _JsonResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_langfuse_trace_fallback_restores_persisted_conversation(monkeypatch) -> None:
    captured_authorization: list[str] = []

    def fake_urlopen(request, timeout):
        assert timeout == 10
        assert request.full_url == (
            "http://localhost:3000/api/public/traces/trace-persisted"
        )
        captured_authorization.append(request.get_header("Authorization"))
        return _JsonResponse(
            {
                "input": {"role": "user", "content": "user-content-sentinel"},
                "output": {
                    "role": "assistant",
                    "content": "assistant-content-sentinel",
                },
                "observations": [
                    {
                        "name": "llm.chat",
                        "input": {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "user-content-sentinel",
                                }
                            ]
                        },
                        "output": {
                            "normalized_result": {
                                "response_text": "assistant-content-sentinel"
                            }
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr(agentruntime_view, "urlopen", fake_urlopen)
    trace = agentruntime_view._get_langfuse_trace(
        "trace-persisted",
        env={
            "LANGFUSE_PUBLIC_KEY": "pk-local",
            "LANGFUSE_SECRET_KEY": "sk-local",
        },
    )

    assert trace is not None
    conversation = agentruntime_view._conversation_from_langfuse_trace(
        "trace-persisted", trace
    )
    assert conversation["source"] == "langfuse_public_api"
    assert conversation["user"]["text"] == "user-content-sentinel"
    assert conversation["assistant"]["text"] == "assistant-content-sentinel"
    assert (
        conversation["llm_outputs"][0]["normalized_result"]["response_text"]
        == "assistant-content-sentinel"
    )
    assert captured_authorization == ["Basic cGstbG9jYWw6c2stbG9jYWw="]
