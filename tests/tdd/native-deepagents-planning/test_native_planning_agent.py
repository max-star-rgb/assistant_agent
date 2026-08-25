from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

from deepagents.backends import FilesystemBackend
from langchain_core.runnables import RunnableLambda
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import build_fast_agent
from assistant_agent.native_agent.planning_agent import build_planning_agent
from assistant_agent.native_agent.providers import MockAssistantChatModel
from deepagents.middleware import SubAgentMiddleware


def _tool_names(raw_tools: object) -> set[str]:
    if not isinstance(raw_tools, list):
        return set()
    return {
        function["name"]
        for item in raw_tools
        if isinstance(item, dict)
        and isinstance((function := item.get("function")), dict)
        and isinstance(function.get("name"), str)
    }


def _last_human_text(messages: list[AnyMessage]) -> str:
    return next(
        str(message.content)
        for message in reversed(messages)
        if isinstance(message, HumanMessage)
    )


class _ParallelTaskModel(MockAssistantChatModel):
    _parent_calls: int = PrivateAttr(default=0)
    _active_subagents: int = PrivateAttr(default=0)
    _max_subagents: int = PrivateAttr(default=0)
    _subagent_calls: Counter[str] = PrivateAttr(default_factory=Counter)
    _all_started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def max_subagents(self) -> int:
        return self._max_subagents

    @property
    def subagent_calls(self) -> Counter[str]:
        return self._subagent_calls

    def _response_message(self, messages: list[AnyMessage], **kwargs: Any) -> AIMessage:
        if {"task", "write_todos"} <= _tool_names(kwargs.get("tools")):
            self._parent_calls += 1
            if self._parent_calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": description,
                                "subagent_type": "general-purpose",
                            },
                            "id": f"task-{description}",
                            "type": "tool_call",
                        }
                        for description in ("alpha", "beta")
                    ],
                )
            return AIMessage(content="native-planning-final")
        return AIMessage(content=f"subagent:{_last_human_text(messages)}")

    async def _agenerate(
        self,
        messages: list[AnyMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if {"task", "write_todos"} <= _tool_names(kwargs.get("tools")):
            return self._generate(
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )
        description = _last_human_text(messages)
        self._subagent_calls[description] += 1
        self._active_subagents += 1
        self._max_subagents = max(self._max_subagents, self._active_subagents)
        if self._active_subagents == 2:
            self._all_started.set()
        try:
            await asyncio.wait_for(self._all_started.wait(), timeout=2)
            return ChatResult(
                generations=[
                    ChatGeneration(message=AIMessage(content=f"subagent:{description}"))
                ]
            )
        finally:
            self._active_subagents -= 1


def test_planning_agent_is_official_model_tools_loop(monkeypatch) -> None:
    from assistant_agent.native_agent import planning_agent as planning_agent_module

    captured: list[object] = []
    real_create_agent = planning_agent_module.create_agent

    def recording_create_agent(*args: Any, **kwargs: Any):
        captured.extend(kwargs["middleware"])
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(planning_agent_module, "create_agent", recording_create_agent)
    model = MockAssistantChatModel()
    fast_agent = build_fast_agent(model, [])
    graph = build_planning_agent(model, fast_agent)
    nodes = set(graph.get_graph().nodes)

    assert {"model", "tools"} <= nodes
    assert not {"supervisor", "controls", "worker", "join"} & nodes
    assert any(isinstance(item, TodoListMiddleware) for item in captured)
    subagent = next(item for item in captured if isinstance(item, SubAgentMiddleware))
    assert subagent.subagent_names == frozenset({"general-purpose"})
    assert [tool.name for tool in subagent.tools] == ["task"]


def test_real_task_tool_runs_compiled_fast_subagents_in_parallel() -> None:
    model = _ParallelTaskModel()
    fast_agent = build_fast_agent(model, [])
    graph = build_planning_agent(model, fast_agent)

    result = asyncio.run(
        graph.ainvoke(
            {
                "messages": [HumanMessage(content="request-sentinel")],
                "memory_context": (),
                "memory_status": "empty",
                "execution_mode": "planning",
            },
            context=AssistantRunContext(),
        )
    )

    assert model.max_subagents == 2
    assert model.subagent_calls == Counter({"alpha": 1, "beta": 1})
    task_results = {
        message.tool_call_id: message.content
        for message in result["messages"]
        if isinstance(message, ToolMessage)
        and message.tool_call_id in {"task-alpha", "task-beta"}
    }
    assert task_results == {
        "task-alpha": "subagent:alpha",
        "task-beta": "subagent:beta",
    }
    assert result["messages"][-1].content == "native-planning-final"


class _PlannerLoadsSkillBeforeTaskModel(MockAssistantChatModel):
    _planning_calls: int = PrivateAttr(default=0)

    def _response_message(self, messages: list[AnyMessage], **kwargs: Any) -> AIMessage:
        if {"task", "write_todos", "load_skill"} <= _tool_names(kwargs.get("tools")):
            self._planning_calls += 1
            if self._planning_calls == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "load_skill",
                            "args": {"skill_id": "travel-sentinel"},
                            "id": "planner-load-skill",
                            "type": "tool_call",
                        }
                    ],
                )
            if self._planning_calls == 2:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {
                                "description": "worker-sentinel",
                                "subagent_type": "general-purpose",
                            },
                            "id": "planner-task",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="planner-final")
        raise AssertionError("only the planning coordinator should call this model")


def _skills_backend(root: Path) -> FilesystemBackend:
    skill_dir = root / "travel-sentinel"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: travel-sentinel\n"
        "description: Travel sentinel.\n"
        "---\n\n"
        "INHERITED-SKILL-SENTINEL\n",
        encoding="utf-8",
    )
    return FilesystemBackend(root_dir=root, virtual_mode=True)


def test_planner_loaded_skill_state_is_mapped_into_worker_without_writeback(
    tmp_path: Path,
) -> None:
    observed_worker_states: list[dict[str, Any]] = []

    def worker(state: dict[str, Any]) -> dict[str, Any]:
        observed_worker_states.append(state)
        return {
            "messages": [AIMessage(content="worker-complete")],
            "loaded_skill_ids": ["worker-only-skill"],
            "skill_reference_grants": {"worker-only-skill": ["reference"]},
            "active_tool_profile_ids": ["worker-only-profile"],
        }

    graph = build_planning_agent(
        _PlannerLoadsSkillBeforeTaskModel(),
        RunnableLambda(worker),
        skills_backend=_skills_backend(tmp_path / "skills"),
    )
    result = graph.invoke(
        {
            "messages": [HumanMessage(content="request-sentinel")],
            "memory_context": (),
            "memory_status": "empty",
            "execution_mode": "planning",
        },
        context=AssistantRunContext(),
    )

    assert len(observed_worker_states) == 1
    worker_state = observed_worker_states[0]
    assert worker_state["loaded_skill_ids"] == ["travel-sentinel"]
    assert worker_state["skill_reference_grants"] == {"travel-sentinel": []}
    assert "planner_loaded_skill_ids" not in worker_state
    assert "planner_skill_reference_grants" not in worker_state
    assert result["planner_loaded_skill_ids"] == ["travel-sentinel"]
    assert "loaded_skill_ids" not in result
    assert "skill_reference_grants" not in result
    assert "active_tool_profile_ids" not in result
