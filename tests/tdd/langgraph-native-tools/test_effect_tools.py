from __future__ import annotations

import ast
import asyncio
import base64
import json
from datetime import timedelta
from pathlib import Path

from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.automation.durable_tasks.models import utc_now
from assistant_agent.automation.durable_tasks.service import DurableTaskService
from assistant_agent.automation.durable_tasks.store import InMemoryTaskStore
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.runtime.thread_resources import (
    ThreadResourceConfig,
    ThreadResourceManager,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.adapters import (
    MockCalendarAdapter,
)
from assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools import (
    create_calendar_create_tool,
)
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationRequest,
    ImageGenerationResult,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    create_image_generation_tool,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import create_image_to_3d_tool
from assistant_agent.tools.plugins.builtin.lodging.watch_tool import (
    create_hotel_price_watch_create_tool,
)


EFFECT_TOOL_MODULES = (
    "assistant_agent.tools.plugins.builtin.image_generation.tool",
    "assistant_agent.tools.plugins.builtin.image_to_3d.tool",
    "assistant_agent.tools.plugins.builtin.lodging.watch_tool",
    "assistant_agent.tools.plugins.builtin.calendar_weather_contacts.tools",
)
PNG_BYTES = b"\x89PNG\r\n\x1a\neffect-tool"


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _GeneratedImageAdapter:
    def __init__(self, output_ref: str) -> None:
        self.output_ref = output_ref

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        return ImageGenerationResult(
            task_id="effect-image-task",
            status="succeeded",
            prompt=request.prompt,
            image_id=["cake"],
            download_url=self.output_ref,
            download_urls=[self.output_ref],
            output_ref=self.output_ref,
        )


def test_effect_tools_are_direct_native_handlers_without_compatibility_wrapper() -> None:
    for module_name in EFFECT_TOOL_MODULES:
        imported = _imported_names(module_name)
        assert "ToolResult" not in imported
        assert "invoke_native_tool" not in imported


def test_effect_tools_hide_runtime_parameters_and_keep_native_handoffs(tmp_path: Path) -> None:
    manager = ThreadResourceManager(ThreadResourceConfig(root=tmp_path / "threads"))
    resources = manager.resolve("user-sentinel", "thread")
    generated = resources.artifact_root / "generated"
    generated.mkdir()
    (generated / "cake.png").write_bytes(PNG_BYTES)
    output_ref = f"/artifacts/{resources.thread_ref}/generated/cake.png"

    image_tool = create_image_generation_tool(
        _GeneratedImageAdapter(output_ref), thread_resource_manager=manager
    )
    image = _invoke(image_tool, {"prompt": "cake"})
    assert "runtime" not in image_tool.args
    assert json.loads(image.content[0]["text"]) == {
        "summary": "Image generation succeeded."
    }
    image_block = next(block for block in image.content if block["type"] == "image")
    assert base64.b64decode(image_block["base64"]) == PNG_BYTES
    assert image.artifact["images"] == [
        {
            "image_id": "cake",
            "output_ref": output_ref,
            "mime_type": "image/png",
        }
    ]
    assert "base64" not in image.artifact["images"][0]

    three_d_tool = create_image_to_3d_tool()
    three_d = _invoke(three_d_tool, {"src_image": "cake"})
    assert "runtime" not in three_d_tool.args
    assert three_d.status == "success"
    assert three_d.artifact["status"] == "generating"
    assert three_d.artifact["source_image_id"] == "cake"
    assert three_d.artifact["job_id"].startswith("image-to-3d-")

    watch_tool = create_hotel_price_watch_create_tool(_durable_service())
    watch = _invoke(
        watch_tool,
        {
            "search": {
                "destination": "Shanghai",
                "check_in": "2026-09-10",
                "check_out": "2026-09-12",
            },
            "max_nightly_price": 500,
            "ends_at": (utc_now() + timedelta(minutes=5)).isoformat(),
        },
    )
    assert "runtime" not in watch_tool.args
    assert watch.status == "success"
    task = watch.artifact["task"]
    assert task["submission_status"] == "accepted"
    assert task["task_id"].startswith("task_")
    assert task["progress_url"] == f"/tasks/{task['task_id']}/events"


def test_calendar_create_keeps_runtime_idempotency_without_compatibility_wrapper() -> None:
    tool = create_calendar_create_tool(MockCalendarAdapter())
    message = _invoke(
        tool,
        {"title": "Native meeting", "start_time": "2026-09-02T09:00:00+08:00"},
    )

    assert "runtime" not in tool.args
    assert message.status == "success"
    assert message.artifact["idempotency"] == {
        "key": "native:thread:run:call-calendar_create",
        "present": True,
        "required": True,
    }


def _durable_service() -> DurableTaskService:
    return DurableTaskService(
        store=InMemoryTaskStore(),
        allowed_tool_names={"lodging_search"},
        tool_side_effect_levels={"lodging_search": "external_read"},
    )


def _invoke(tool: BaseTool, args: dict[str, object]) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([tool], handle_tool_errors=lambda error: str(error)))
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
            config={
                "run_id": "run",
                "configurable": {
                    "thread_id": "thread",
                    "langgraph_auth_user": _User(),
                }
            },
        )
    )
    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message


def _imported_names(module_name: str) -> set[str]:
    module_path = Path(__file__).parents[3] / "src" / (module_name.replace(".", "/") + ".py")
    names: set[str] = set()
    for node in ast.walk(ast.parse(module_path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names
