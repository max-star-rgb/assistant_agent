import json

from assistant_agent.tools.native_boundary import (
    native_content_and_artifact,
    native_tool_exception,
)


def test_native_projection_returns_standard_content_and_artifact() -> None:
    content, artifact = native_content_and_artifact(
        {"status": "ok"}, {"value": "artifact-sentinel"}
    )
    assert json.loads(content[0]["text"]) == {"status": "ok"}
    assert artifact == {"value": "artifact-sentinel"}


def test_native_tool_exception_redacts_sensitive_details() -> None:
    error = native_tool_exception(
        RuntimeError("api_key=secret-sentinel path=/home/private/result.json")
    )
    assert "secret-sentinel" not in str(error)
    assert "/home/private" not in str(error)
