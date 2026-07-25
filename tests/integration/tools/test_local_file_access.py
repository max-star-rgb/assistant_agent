"""Governed local text-file access contracts."""

from pathlib import Path

from assistant_agent.agent.runtime import AgentGraphRuntime
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.assistant_decision import NativeToolCall
from assistant_agent.schemas.file_access import FileReadRequest
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.chat_adapter import ChatRequest, ChatResult
from assistant_agent.services.session_store import InMemorySessionStore
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.plugins.builtin.local_file_access.tool import (
    LocalFileReadTool,
)


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-file-read"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def test_file_read_returns_bounded_utf8_content_and_cursor(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("你好，文件总结。" * 10, encoding="utf-8")
    tool = LocalFileReadTool(root=tmp_path)

    first = tool.run(FileReadRequest(path="notes.md", max_chars=12))

    assert first.success is True
    assert first.data is not None
    assert first.data["content"] == source.read_text(encoding="utf-8")[:12]
    assert first.data["truncated"] is True
    assert first.data["next_cursor"] == 12
    assert first.model_observation is not None
    assert first.model_observation["content"] == first.data["content"]

    second = tool.run(
        FileReadRequest(
            path="notes.md",
            cursor=first.data["next_cursor"],
            max_chars=50_000,
        )
    )

    assert second.success is True
    assert second.data is not None
    assert second.data["truncated"] is False
    assert second.data["next_cursor"] is None


def test_file_read_rejects_path_traversal_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)
    tool = LocalFileReadTool(root=root)

    traversal = tool.run({"path": "../outside.txt"})
    symlink = tool.run({"path": "escape.txt"})

    assert traversal.success is False
    assert traversal.error is not None
    assert traversal.error.startswith("file_path_invalid:")
    assert symlink.success is False
    assert symlink.error is not None
    assert symlink.error.startswith("file_access_denied:")


def test_file_read_rejects_unsupported_type_encoding_and_oversize(
    tmp_path: Path,
) -> None:
    (tmp_path / "image.png").write_bytes(b"not an image")
    (tmp_path / "legacy.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "large.txt").write_text("12345", encoding="utf-8")
    tool = LocalFileReadTool(root=tmp_path, max_file_bytes=4)

    unsupported = tool.run({"path": "image.png"})
    encoding = tool.run({"path": "legacy.txt"})
    oversize = tool.run({"path": "large.txt"})

    assert unsupported.error is not None
    assert unsupported.error.startswith("file_type_unsupported:")
    assert encoding.error is not None
    assert encoding.error.startswith("file_encoding_unsupported:")
    assert oversize.error is not None
    assert oversize.error.startswith("file_too_large:")


def test_default_registry_exposes_file_read_with_runtime_owned_limit(
    tmp_path: Path,
) -> None:
    config = ProviderConfig(local_file_access_root=str(tmp_path))

    registry = create_default_registry(config, plugin_modules=[])
    spec = registry.get_spec("file_read")
    tool = registry.get("file_read")

    assert registry.registration_record("file_read").plugin_id == "local_file_access"
    assert spec.category == "read"
    assert spec.requires_confirmation is False
    assert set(spec.input_schema["properties"]) == {"path", "cursor"}
    assert tool.root == tmp_path.resolve()


def test_file_access_root_can_be_configured_from_environment(tmp_path: Path) -> None:
    config = ProviderConfig.from_env(
        {"MULTIMODAL_AGENT_FILE_ACCESS_ROOT": str(tmp_path)}
    )

    assert config.local_file_access_root == str(tmp_path)


def test_runtime_reads_file_then_exposes_content_for_summary(tmp_path: Path) -> None:
    sentinel = "FILE_CONTENT_FOR_SUMMARY"
    (tmp_path / "brief.md").write_text(sentinel, encoding="utf-8")
    config = ProviderConfig(
        local_file_access_root=str(tmp_path),
        langgraph_checkpointer_backend="none",
    )
    registry = create_default_registry(config, plugin_modules=[])
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-file-read",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="file-read-call",
                        name="file_read",
                        arguments={"path": "brief.md"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-file-read",
                finish_reason="stop",
                response_text="文件摘要已生成。",
            ),
        ]
    )
    runtime = AgentGraphRuntime(
        config=config,
        registry=registry,
        chat_adapter=adapter,
        session_store=InMemorySessionStore(),
    )

    state = runtime.run_state(
        UserRequest(
            user_id="file-user",
            session_id="file-session",
            text="请总结 brief.md",
            metadata={"tool_visibility": {"enabled_tools": ["file_read"]}},
        )
    )

    assert state.status == "completed"
    assert state.response.message == "文件摘要已生成。"
    assert state.tool_results[0].model_observation is not None
    assert state.tool_results[0].model_observation["content"] == sentinel
    assert sentinel in str(adapter.requests[1].messages)
