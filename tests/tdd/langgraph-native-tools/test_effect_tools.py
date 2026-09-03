from __future__ import annotations

import ast
import asyncio
import json
from datetime import timedelta
from pathlib import Path

from langchain.agents import AgentState
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
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
from assistant_agent.tools.ids import IMAGE_GENERATION_TOOL_NAME
from assistant_agent.tools.plugins.builtin.image_to_3d.tool import (
    MockImageTo3DAdapter,
    _execute_image_to_3d,
    _latest_generated_image_ref,
    create_image_to_3d_tool,
)
from assistant_agent.tools.plugins.builtin.image_to_3d.models import ImageTo3DRequest
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
    output_ref = f"artifact://v1/{resources.thread_ref}/generated/cake.png"

    image_tool = create_image_generation_tool(
        _GeneratedImageAdapter(output_ref), thread_resource_manager=manager
    )
    image = _invoke(image_tool, {"prompt": "cake"})
    assert "runtime" not in image_tool.args
    assert json.loads(image.content[0]["text"]) == {
        "images": [{"image_id": "cake", "url": output_ref}]
    }
    assert [block["type"] for block in image.content] == ["text"]
    assert image.artifact["images"] == [
        {
            "image_id": "cake",
            "output_ref": output_ref,
            "mime_type": "image/png",
            "url": output_ref,
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


def test_image_to_3d_reuses_the_generation_artifact_ref_across_turns() -> None:
    output_ref = "artifact://v1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/generated/cake.png"
    message = ToolMessage(
        content="generated",
        name=IMAGE_GENERATION_TOOL_NAME,
        tool_call_id="call-image",
        artifact={"images": [{"image_id": "cake", "output_ref": output_ref}]},
    )

    state = {
        "messages": [
            message,
            HumanMessage(content="把上一张图片转成 3D"),
        ]
    }

    assert _latest_generated_image_ref(state) == output_ref


def test_image_to_3d_keeps_an_explicit_artifact_selection() -> None:
    explicit_ref = "artifact://v1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/generated/first.png"
    latest_ref = "artifact://v1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/generated/latest.png"

    submission = _execute_image_to_3d(
        MockImageTo3DAdapter(),
        ImageTo3DRequest(src_image=explicit_ref),
        user_id="user",
        session_id="thread",
        latest_image_ref=latest_ref,
    )

    assert submission.source_image_id == explicit_ref


def test_calendar_create_keeps_runtime_idempotency_without_compatibility_wrapper() -> None:
    adapter = _RecordingCalendarAdapter()
    tool = create_calendar_create_tool(adapter)
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
    assert adapter.created_event_titles == ["Native meeting"]
    assert adapter.received_idempotency_keys == [
        "native:thread:run:call-calendar_create"
    ]


def test_effect_toolnode_validation_errors_do_not_echo_input_values(tmp_path: Path) -> None:
    manager = ThreadResourceManager(ThreadResourceConfig(root=tmp_path / "threads"))
    image = _invoke(
        create_image_generation_tool(thread_resource_manager=manager),
        {"prompt": " "},
    )
    watch = _invoke(
        create_hotel_price_watch_create_tool(_durable_service()),
        {
            "search": {
                "destination": "hotel-input-sentinel",
                "check_in": "2026-09-10",
                "check_out": "2026-09-12",
            },
            "max_nightly_price": 500,
            "ends_at": "2026-09-02T01:02:03",
        },
    )
    three_d = _invoke(create_image_to_3d_tool(), {"src_image": " "})

    for message in (image, watch, three_d):
        assert message.status == "error"
        assert "input_value" not in message.content
    assert "ImageGenerationRequest" not in image.content
    assert "HotelPriceWatchGoal" not in watch.content
    assert "hotel-input-sentinel" not in watch.content
    assert three_d.content == "image_to_3d requires a generated image"


def test_effect_toolnode_schema_errors_do_not_echo_tool_call_kwargs(tmp_path: Path) -> None:
    manager = ThreadResourceManager(ThreadResourceConfig(root=tmp_path / "threads"))
    image = _invoke(
        create_image_generation_tool(thread_resource_manager=manager),
        {"prompt": {"raw": "image-schema-sentinel"}},
    )
    three_d = _invoke(
        create_image_to_3d_tool(),
        {"src_image": {"raw": "three-d-schema-sentinel"}},
    )
    watch = _invoke(
        create_hotel_price_watch_create_tool(_durable_service()),
        {
            "search": {
                "destination": "hotel-schema-sentinel",
                "check_in": "2026-09-10",
                "check_out": "2026-09-12",
            },
            "max_nightly_price": 500,
        },
    )

    for message, sentinel in (
        (image, "image-schema-sentinel"),
        (three_d, "three-d-schema-sentinel"),
        (watch, "hotel-schema-sentinel"),
    ):
        assert message.status == "error"
        assert sentinel not in message.content
        assert "input_value" not in message.content


def _durable_service() -> DurableTaskService:
    return DurableTaskService(
        store=InMemoryTaskStore(),
        allowed_tool_names={"lodging_search"},
        tool_side_effect_levels={"lodging_search": "external_read"},
    )


class _RecordingCalendarAdapter(MockCalendarAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.received_idempotency_keys: list[str | None] = []

    def create(self, request):  # type: ignore[no-untyped-def]
        self.received_idempotency_keys.append(request.idempotency_key)
        return super().create(request)


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
                    "langgraph_auth_user": _User()
                },
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
