import io
import json
import urllib.error

import pytest

from assistant_agent.agent.action_validator import ActionValidator
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.assistant_decision import AssistantDecision
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tool_observation import observation_from_tool_result
from assistant_agent.schemas.visual_image_search import VisualImageSearchRequest
from assistant_agent.services.tool_visual_image_search_adapter import (
    MockVisualImageSearchAdapter,
    QwenImageSearchAdapter,
    QwenImageSearchConfig,
)
from assistant_agent.tools.registry import create_default_registry
from assistant_agent.tools.visual_image_search_tool import VisualImageSearchTool


def _validate_visual_image_search(tool_input: dict[str, object]):
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="用这张图在网上找相似图片",
    )
    return ActionValidator().validate(
        decision=AssistantDecision(
            type="tool_call",
            tool_name="visual_image_search",
            tool_input=tool_input,
        ),
        registry=create_default_registry(),
        request=request,
        state=AgentState.from_request(request),
    )


def test_default_registry_includes_visual_image_search_as_external_read() -> None:
    registry = create_default_registry()

    assert "visual_image_search" in registry.list()
    spec = registry.get_spec("visual_image_search")
    assert spec.side_effect.level == "external_read"
    assert spec.side_effect.requires_confirmation is False
    assert spec.execution.dependency_mode == "independent"
    assert spec.execution.resource_reads == ["media:image", "web_image_search"]
    assert spec.execution.realtime_safety == "safe"
    assert spec.execution.artifact_reuse == "reusable"
    assert "image_url" in spec.input_schema["fields"]
    assert "image_ids" in spec.input_schema["fields"]


def test_action_validator_rejects_missing_visual_image_input() -> None:
    validation = _validate_visual_image_search({"query_hint": "same jacket"})

    assert validation.accepted is False
    assert validation.code == "missing_required_input"
    assert validation.message == "visual_image_search requires image_url or image_ids."


@pytest.mark.parametrize(
    "tool_input",
    [
        {"image_url": "/home/lenovo1/private/cat.jpg"},
        {"image_url": "file:///tmp/cat.jpg"},
        {"image_ids": ["img-local-1"]},
    ],
)
def test_action_validator_rejects_non_http_visual_image_references(
    tool_input: dict[str, object],
) -> None:
    validation = _validate_visual_image_search(tool_input)

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert validation.message == (
        "visual_image_search v1 only supports public http or https image URLs."
    )
    assert "/home/lenovo1/private/cat.jpg" not in validation.message


def test_action_validator_accepts_http_visual_image_url() -> None:
    validation = _validate_visual_image_search(
        {"image_url": "https://example.com/cat.jpg", "limit": 3}
    )

    assert validation.accepted is True
    assert validation.code == "accepted"


def test_action_validator_rejects_visual_image_search_limit_outside_schema() -> None:
    validation = _validate_visual_image_search(
        {"image_url": "https://example.com/cat.jpg", "limit": 25}
    )

    assert validation.accepted is False
    assert validation.code == "invalid_tool_input"
    assert "less than or equal to 10" in validation.message


def test_mock_visual_image_search_tool_returns_structured_result_and_contract() -> None:
    result = VisualImageSearchTool(adapter=MockVisualImageSearchAdapter()).run(
        {
            "image_url": "https://example.com/catalog/jacket.jpg",
            "query_hint": "same jacket",
            "limit": 2,
        }
    )

    assert result.success is True
    assert result.tool_name == "visual_image_search"
    assert result.output_ref == "mock://visual_image_search/example-com-catalog-jacket-jpg"
    assert result.data["provider"] == "mock"
    assert result.data["image_used"] == "https://example.com/catalog/jacket.jpg"
    assert result.data["query_hint_used"] == "same jacket"
    assert len(result.data["matches"]) == 2
    assert result.data["matches"][0]["page_url"].startswith("https://mock.example/")
    assert result.data["matches"][0]["image_url"].startswith("https://mock.example/")
    assert result.contract is not None
    assert result.contract.capability == "visual_image_search"
    assert result.contract.status == "succeeded"


def test_visual_image_search_observation_is_prompt_safe() -> None:
    result = VisualImageSearchTool(adapter=MockVisualImageSearchAdapter()).run(
        {"image_url": "https://example.com/catalog/jacket.jpg", "limit": 1}
    )

    observation = observation_from_tool_result(result)

    assert observation.status == "succeeded"
    assert "Found 1 visually similar image result" in observation.summary
    assert observation.structured_output["matches"][0]["title"]
    assert "provider" in result.data
    assert "provider" not in observation.structured_output
    assert "latency_ms" not in observation.structured_output


class _FakeResponse:
    def __init__(self, payload: str, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return self.payload.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def test_qwen_image_search_adapter_builds_responses_payload_and_parses_results(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            json.dumps(
                {
                    "id": "resp_123",
                    "output": [
                        {
                            "type": "image_search_call",
                            "status": "completed",
                            "output": json.dumps(
                                [
                                    {
                                        "index": 1,
                                        "title": "Blue jacket product photo",
                                        "url": "https://images.example/jacket.jpg",
                                        "source": "images.example",
                                        "snippet": "A visually similar jacket.",
                                        "score": 0.82,
                                    }
                                ]
                            ),
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(
        "assistant_agent.services.tool_visual_image_search_adapter.urllib.request.urlopen",
        fake_urlopen,
    )

    result = QwenImageSearchAdapter(
        QwenImageSearchConfig(
            api_key="sk-qwen-image-search-test",
            base_url="https://dashscope.local/compatible-mode/v1",
            model="qwen3.7-plus",
            timeout_seconds=4.5,
        )
    ).search(
        VisualImageSearchRequest(
            image_url="https://example.com/jacket.jpg",
            query_hint="same jacket",
            limit=5,
        )
    )

    assert result.success is True
    assert result.provider == "qwen"
    assert result.output_ref == "qwen://image_search/resp_123"
    assert result.matches[0].title == "Blue jacket product photo"
    assert result.matches[0].page_url == "https://images.example/jacket.jpg"
    assert result.matches[0].image_url == "https://images.example/jacket.jpg"
    assert result.matches[0].similarity_score == 0.82
    assert captured["url"] == "https://dashscope.local/compatible-mode/v1/responses"
    assert captured["timeout"] == 4.5
    assert captured["headers"]["Authorization"] == "Bearer sk-qwen-image-search-test"
    payload = captured["payload"]
    assert payload["model"] == "qwen3.7-plus"
    assert payload["tools"] == [{"type": "image_search"}]
    content = payload["input"][0]["content"]
    assert content == [
        {"type": "input_text", "text": "same jacket"},
        {"type": "input_image", "image_url": "https://example.com/jacket.jpg"},
    ]
    assert "sk-qwen-image-search-test" not in result.model_dump_json()


def test_qwen_image_search_adapter_missing_config_returns_provider_unconfigured() -> None:
    result = QwenImageSearchAdapter(
        QwenImageSearchConfig(api_key=None)
    ).search(VisualImageSearchRequest(image_url="https://example.com/cat.jpg"))

    assert result.success is False
    assert result.provider == "qwen"
    assert result.errors[0].code == "provider_unconfigured"
    assert result.errors[0].recoverable is True


@pytest.mark.parametrize(
    ("fake_response", "expected_code"),
    [
        (
            lambda: urllib.error.HTTPError(
                "https://dashscope.local/responses",
                429,
                "Too Many Requests",
                hdrs={},
                fp=io.BytesIO(b'{"message":"limited"}'),
            ),
            "provider_rate_limited",
        ),
        (lambda: TimeoutError("timed out"), "provider_timeout"),
        (lambda: _FakeResponse("{not-json"), "provider_bad_response"),
        (
            lambda: _FakeResponse(json.dumps({"output": [{"type": "message"}]})),
            "provider_schema_mismatch",
        ),
    ],
)
def test_qwen_image_search_adapter_failures_return_structured_errors(
    monkeypatch,
    fake_response,
    expected_code: str,
) -> None:
    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        response_or_error = fake_response()
        if isinstance(response_or_error, BaseException):
            raise response_or_error
        return response_or_error

    monkeypatch.setattr(
        "assistant_agent.services.tool_visual_image_search_adapter.urllib.request.urlopen",
        fake_urlopen,
    )

    result = QwenImageSearchAdapter(
        QwenImageSearchConfig(
            api_key="sk-qwen-image-search-test",
            base_url="https://dashscope.local/compatible-mode/v1",
        )
    ).search(VisualImageSearchRequest(image_url="https://example.com/cat.jpg"))

    assert result.success is False
    assert result.errors[0].code == expected_code
    assert "sk-qwen-image-search-test" not in result.model_dump_json()


def test_visual_image_search_model_observation_does_not_expose_raw_base64_or_paths(
    monkeypatch,
) -> None:
    base64_payload = "data:image/png;base64," + ("A" * 120)

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        return _FakeResponse(
            json.dumps(
                {
                    "id": "resp_123",
                    "output": [
                        {
                            "type": "image_search_call",
                            "status": "completed",
                            "output": json.dumps(
                                [
                                    {
                                        "title": "Unsafe payload",
                                        "url": "https://images.example/safe.jpg",
                                        "snippet": base64_payload,
                                        "source": "/home/lenovo1/private",
                                    }
                                ]
                            ),
                        }
                    ],
                }
            )
        )

    monkeypatch.setattr(
        "assistant_agent.services.tool_visual_image_search_adapter.urllib.request.urlopen",
        fake_urlopen,
    )

    result = VisualImageSearchTool(
        adapter=QwenImageSearchAdapter(
            QwenImageSearchConfig(
                api_key="sk-qwen-image-search-test",
                base_url="https://dashscope.local/compatible-mode/v1",
            )
        )
    ).run({"image_url": "https://example.com/cat.jpg"})

    rendered = str(result.model_observation)

    assert result.success is True
    assert "data:image/png;base64" not in rendered
    assert "/home/lenovo1/private" not in rendered
    assert "sk-qwen-image-search-test" not in rendered
    assert "raw" not in rendered.lower()
