from deepagents.backends import FilesystemBackend, LocalShellBackend
from langchain_core.messages import AIMessage, HumanMessage

from assistant_agent.native_agent.assistant_agent import (
    build_assistant_agent,
    build_general_purpose_worker,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel


def test_native_graph_owns_the_assistant_response(tmp_path) -> None:
    model = MockAssistantChatModel()
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=True)
    skills_backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    worker = build_general_purpose_worker(
        model,
        [],
        backend=backend,
        skills_backend=skills_backend,
    )
    graph = build_assistant_agent(
        model,
        [],
        backend=backend,
        worker_graph=worker,
        skills_backend=skills_backend,
        tool_profiles=(),
    )
    result = graph.invoke(
        {"messages": [HumanMessage(content="response-sentinel")]},
        context=AssistantRunContext(enable_memory=False),
        config={"configurable": {"thread_id": "native-response-thread"}},
    )
    response = result["messages"][-1]

    assert isinstance(response, AIMessage)
    assert response.text == "已收到：response-sentinel"
    assert response.tool_calls == []
