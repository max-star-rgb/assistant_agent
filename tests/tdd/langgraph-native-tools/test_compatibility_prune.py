from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.store import InMemoryTaskStore
from assistant_agent.media.embedding.coordinator_store import SessionEmbeddingCoordinatorStore
from assistant_agent.media.video.semantic_store_pool import SessionVisualSemanticStorePool
from assistant_agent.media.video.visual_memory_index import UnavailableVisualMemoryTextIndex
from assistant_agent.media.video.visual_reminder import VisualReminderRegistry
from assistant_agent.media.vision.vision_client import MockVisionUnderstandingClient
from assistant_agent.runtime.thread_resources import ThreadResourceConfig, ThreadResourceManager
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.tools import native_boundary
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    create_calendar_create_tool,
    create_calendar_search_tool,
    create_contacts_search_tool,
)
from assistant_agent.tools.plugins.builtin.email_access.backend import MockEmailBackend
from assistant_agent.tools.plugins.builtin.email_access.tools import (
    create_email_read_tool,
    create_email_search_tool,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    create_image_generation_tool,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import create_image_to_3d_tool
from assistant_agent.tools.plugins.builtin.local_file_access.tool import (
    create_local_file_read_tool,
)
from assistant_agent.tools.plugins.builtin.lodging.tool import create_lodging_search_tool
from assistant_agent.tools.plugins.builtin.lodging.watch_tool import (
    create_hotel_price_watch_create_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.live_tool import (
    create_live_view_inspect_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import (
    create_uploaded_media_inspect_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_memory_tool import (
    create_visual_memory_search_tool,
)
from assistant_agent.tools.plugins.builtin.media_inspection.visual_reminder_tool import (
    create_visual_reminder_manage_tool,
)
from assistant_agent.tools.plugins.builtin.shopping.tool import create_shopping_search_tool
from assistant_agent.tools.plugins.builtin.visual_image_search.tool import (
    create_visual_image_search_tool,
)
from assistant_agent.tools.plugins.builtin.web_access.fetch_tool import create_web_fetch_tool


COMPATIBILITY_SYMBOLS = frozenset(
    {
        "ToolContext",
        "ToolResult",
        "invoke_native_tool",
        "native_tool_response",
        "tool_context",
    }
)

BUSINESS_TOOL_NAMES = (
    "calendar_create",
    "calendar_search",
    "contacts_search",
    "email_read",
    "email_search",
    "file_read",
    "hotel_price_watch_create",
    "image_generation",
    "image_to_3d",
    "live_view_inspect",
    "lodging_search",
    "shopping_search",
    "uploaded_media_inspect",
    "visual_image_search",
    "visual_memory_search",
    "visual_reminder_manage",
    "web_fetch",
)


def test_builtin_tool_modules_have_no_compatibility_references() -> None:
    references = {
        path.relative_to(_source_root()): _compatibility_references(path)
        for path in _builtin_root().rglob("*.py")
    }

    assert references == {
        path: set() for path in references
    }


def test_legacy_execution_functions_are_not_a_production_tool_boundary() -> None:
    assert not any(
        hasattr(native_boundary, symbol)
        for symbol in ("invoke_native_tool", "native_tool_response")
    )


def test_all_builtin_factories_return_native_tools_with_hidden_runtime(tmp_path: Path) -> None:
    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic")
    try:
        tools = _builtin_factories(tmp_path, semantic_pool)
    finally:
        semantic_pool.close()

    assert len(tools) == 17
    assert {tool.name for tool in tools} == set(BUSINESS_TOOL_NAMES)
    for tool in tools:
        assert isinstance(tool, BaseTool)
        assert "runtime" not in tool.tool_call_schema.model_fields


@pytest.fixture(scope="module")
def business_tools(tmp_path_factory: pytest.TempPathFactory):
    tmp_path = tmp_path_factory.mktemp("business-tools")
    semantic_pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic")
    try:
        yield {tool.name: tool for tool in _builtin_factories(tmp_path, semantic_pool)}
    finally:
        semantic_pool.close()


@pytest.mark.parametrize("tool_name", BUSINESS_TOOL_NAMES)
def test_business_tool_schema_errors_do_not_echo_call_kwargs(
    tool_name: str,
    business_tools: dict[str, BaseTool],
) -> None:
    tool = business_tools[tool_name]
    sentinel = f"{tool_name}-schema-sentinel"
    visible_field = next(iter(tool.tool_call_schema.model_fields))

    message = _invoke_default_toolnode(
        tool,
        {visible_field: {"raw": sentinel}},
    )

    assert message.status == "error"
    assert sentinel not in str(message.content)


def _builtin_factories(
    tmp_path: Path,
    semantic_pool: SessionVisualSemanticStorePool,
) -> tuple[BaseTool, ...]:
    email_backend = MockEmailBackend()
    vision_client = MockVisionUnderstandingClient()
    coordinator_store = SessionEmbeddingCoordinatorStore(factory=lambda *_: None)  # type: ignore[arg-type]
    return (
        create_calendar_search_tool(),
        create_calendar_create_tool(),
        create_contacts_search_tool(),
        create_email_search_tool(email_backend),
        create_email_read_tool(email_backend),
        create_local_file_read_tool(tmp_path),
        create_web_fetch_tool(),
        create_shopping_search_tool(
            search_adapter=object(),  # type: ignore[arg-type]
            compare_adapter=object(),  # type: ignore[arg-type]
        ),
        create_lodging_search_tool(),
        create_hotel_price_watch_create_tool(
            DurableTaskService(
                store=InMemoryTaskStore(),
                allowed_tool_names={"lodging_search"},
            )
        ),
        create_visual_image_search_tool(),
        create_image_generation_tool(
            thread_resource_manager=ThreadResourceManager(
                ThreadResourceConfig(root=tmp_path / "threads")
            )
        ),
        create_image_to_3d_tool(),
        create_uploaded_media_inspect_tool(vision_client),
        create_live_view_inspect_tool(vision_client),
        create_visual_memory_search_tool(
            semantic_store_pool=semantic_pool,
            text_index=UnavailableVisualMemoryTextIndex(code="offline", message="offline"),
        ),
        create_visual_reminder_manage_tool(
            coordinator_store=coordinator_store,
            reminder_registry=VisualReminderRegistry(),
        ),
    )


def _compatibility_references(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        name
        for node in ast.walk(tree)
        for name in _node_names(node)
        if name in COMPATIBILITY_SYMBOLS
    }


def _node_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (node.attr,)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return tuple(alias.name for alias in node.names)
    return ()


def _source_root() -> Path:
    return Path(__file__).parents[3] / "src"


def _builtin_root() -> Path:
    return _source_root() / "assistant_agent/tools/plugins/builtin"


def _invoke_default_toolnode(
    tool: BaseTool,
    args: dict[str, object],
) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    result = asyncio.run(
        builder.compile().ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": tool.name,
                                "args": args,
                                "id": f"call-{tool.name}",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            },
            context=AssistantRunContext(),
        )
    )
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message
