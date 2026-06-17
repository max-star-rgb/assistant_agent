from scripts.run_demo_flows import _sanitize_payload, run_demo_flows


def test_image_generation_basic_demo_flow_uses_image_generation_tool() -> None:
    summary = run_demo_flows(scenario_id="image_generation_basic")

    assert summary["failed"] == 0
    result = summary["results"][0]
    assert result["tool_sequence"] == ["image_generation"]
    assert result["response_text"] != "已完成请求处理。"
    assert "图片" in result["response_text"]
    assert "生成" in result["response_text"]
    assert result["errors"] == []
    assert result["checks"]["expected_tools_match"] is True
    assert result["checks"]["response_contains_match"] is True
    assert result["checks"]["non_generic_response"] is True


def test_demo_flow_sanitizer_redacts_image_generation_provider_secrets_and_raw_response() -> None:
    payload = {
        "authorization": "Bearer dashscope-secret-key",
        "raw": {"provider_response": {"image": "data:image/png;base64," + "a" * 120}},
        "message": "Authorization: Bearer dashscope-secret-key",
    }

    sanitized = _sanitize_payload(payload)

    assert sanitized["authorization"] == "[redacted]"
    assert sanitized["raw"] == "[redacted]"
    assert sanitized["message"] == "[redacted]"
    assert "dashscope-secret-key" not in str(sanitized)
    assert "base64" not in str(sanitized)
