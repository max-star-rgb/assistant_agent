import json
from pathlib import Path

from langchain.agents import AgentState
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.runtime.thread_resources import (
    ThreadResourceConfig,
    ThreadResourceManager,
)
from assistant_agent.tools.plugins.builtin.image_generation.models import (
    ImageGenerationRequest,
    ImageGenerationResult,
)
from assistant_agent.tools.plugins.builtin.image_generation.tool import (
    create_image_generation_tool,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage-generation-studio"


class _User(dict):
    identity = "user-sentinel"
    permissions = ()


class _GeneratedImageAdapter:
    def __init__(self, output_ref: str) -> None:
        self.output_ref = output_ref

    def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        return ImageGenerationResult(
            task_id="cake-task",
            status="succeeded",
            prompt=request.prompt,
            image_id=["cake"],
            download_url=self.output_ref,
            download_urls=[self.output_ref],
            output_ref=self.output_ref,
            provider_image_urls=["https://provider.example/cake.png?signature=secret"],
        )


def test_model_observation_exposes_only_backend_owned_image_url(tmp_path) -> None:
    manager = ThreadResourceManager(ThreadResourceConfig(root=tmp_path / "threads"))
    resources = manager.resolve("user-sentinel", "thread-sentinel")
    generated = resources.artifact_root / "generated"
    generated.mkdir()
    (generated / "cake.png").write_bytes(PNG_BYTES)
    output_ref = f"artifact://v1/{resources.thread_ref}/generated/cake.png"
    message = _invoke_image_tool(
        create_image_generation_tool(
            _GeneratedImageAdapter(output_ref),
            thread_resource_manager=manager,
        )
    )
    observation = json.loads(message.content[0]["text"])

    assert "image_id" not in observation
    assert observation["images"] == [
        {
            "image_id": "cake",
            "url": output_ref,
        }
    ]
    assert message.artifact["images"][0]["url"] == observation["images"][0]["url"]
    assert message.artifact["assistant_agent_delivery_v1"]["output_refs"] == [
        output_ref
    ]
    assert "provider.example" not in message.content[0]["text"]


def test_image_generation_keeps_binary_out_of_tool_message(tmp_path: Path) -> None:
    manager = ThreadResourceManager(ThreadResourceConfig(root=tmp_path / "threads"))
    resources = manager.resolve("user-sentinel", "thread-sentinel")
    generated = resources.artifact_root / "generated"
    generated.mkdir()
    (generated / "cake.png").write_bytes(PNG_BYTES)
    output_ref = f"artifact://v1/{resources.thread_ref}/generated/cake.png"
    message = _invoke_image_tool(
        create_image_generation_tool(
            _GeneratedImageAdapter(output_ref),
            thread_resource_manager=manager,
        )
    )
    assert [block["type"] for block in message.content] == ["text"]
    assert message.artifact["images"][0]["output_ref"] == output_ref


def _invoke_image_tool(image_tool) -> ToolMessage:
    builder = StateGraph(AgentState, context_schema=AssistantRunContext)
    builder.add_node("tools", ToolNode([image_tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)

    result = builder.compile().invoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "image_generation",
                            "args": {"prompt": "cake"},
                            "id": "image-call",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        },
        context=AssistantRunContext(),
        config={
            "configurable": {
                "thread_id": "thread-sentinel",
                "langgraph_auth_user": _User(),
            }
        },
    )

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    return message
