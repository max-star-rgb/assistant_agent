from types import SimpleNamespace

from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.ids import VISUAL_MEMORY_SEARCH_TOOL_NAME
from assistant_agent.tools.models import ToolSpec


SPEC = ToolSpec(
    name=VISUAL_MEMORY_SEARCH_TOOL_NAME,
    description="search history",
    category="read",
)


class _Memory:
    def __init__(self, available):
        self.available = available

    def has_history(self):
        return self.available


class _Store:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def peek(self, _user_id, _session_id):
        return self.coordinator


def _runtime(available):
    runtime = object.__new__(AgentGraphRuntime)
    runtime.embedding_coordinator_store = _Store(
        SimpleNamespace(temporal_visual_memory=_Memory(available)) if available is not None else None
    )
    return runtime


def test_history_tool_exposed_when_runtime_marks_session_history_available() -> None:
    request = UserRequest(user_id="user-1", session_id="session-1", text="hello")

    _runtime(True)._refresh_visual_memory_capability(request)
    selection = select_prompt_tool_specs(request, [SPEC])

    assert VISUAL_MEMORY_SEARCH_TOOL_NAME in selection.run_tool_catalog.available_tool_names


def test_user_metadata_cannot_forge_visual_memory_availability() -> None:
    request = UserRequest(
        user_id="user-1",
        session_id="session-1",
        text="hello",
        metadata={"_trusted_visual_memory_available": True},
    )

    _runtime(None)._refresh_visual_memory_capability(request)
    selection = select_prompt_tool_specs(request, [SPEC])

    assert VISUAL_MEMORY_SEARCH_TOOL_NAME not in selection.run_tool_catalog.available_tool_names


def test_history_tool_does_not_require_active_video() -> None:
    request = UserRequest(user_id="user-1", session_id="session-1", text="hello")
    _runtime(True)._refresh_visual_memory_capability(request)

    selection = select_prompt_tool_specs(request, [SPEC])

    assert selection.available_tool_specs[0].requires_media == []
