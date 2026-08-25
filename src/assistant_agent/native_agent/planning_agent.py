"""Planning agent assembled from upstream LangChain and Deep Agents middleware."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from deepagents.backends import StateBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware import CompiledSubAgent, SubAgentMiddleware
from deepagents.middleware.skills import SkillMetadata
from langchain.agents import create_agent
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import MessageLikeRepresentation
from langchain_core.runnables import RunnableConfig, RunnableLambda

from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.fast_agent import (
    RuntimeContextMiddleware,
    ToolProgressMiddleware,
)
from assistant_agent.native_agent.providers import planning_supervisor_model_view
from assistant_agent.native_agent.state import PlanningAgentState
from assistant_agent.native_agent.tool_call_limits import PerToolCallLimitMiddleware
from assistant_agent.skills.native import (
    create_project_skills_backend,
    create_project_skills_middleware,
    load_project_skills_metadata,
)
from assistant_agent.tools.plugins.builtin.skill_loading.tool import (
    create_load_skill_reference_tool,
    create_load_skill_tool,
)


_WRITE_TODOS_DESCRIPTION_ZH = """创建并管理当前工作会话的结构化待办列表。

只在复杂、多步骤任务中使用。开始执行前把当前事项标记为 in_progress，完成后立即标记为 completed；
如果遇到错误或阻塞，保持 in_progress 并记录需要解决的新事项。简单任务应直接完成，不必创建待办列表。
最后一次更新待办后，还必须另发一条消息交付用户实际要求的结果。"""

_WRITE_TODOS_SYSTEM_PROMPT_ZH = """## `write_todos`

你可以使用 `write_todos` Tool 管理和规划复杂目标。对于复杂、多步骤目标，应使用该 Tool 跟踪每个必要步骤，
并把较大的目标拆分为更小、更明确的 Todo。

完成一个步骤后，必须立即把对应 Todo 标记为 completed，不要积攒多个已完成步骤后再批量更新。
对于只需少量步骤的简单目标，应直接完成，不要调用 `write_todos`。创建和维护 Todo 会消耗时间与 token，
仅在它确实有助于管理复杂任务时使用。

## Todo 使用规则

- 同一个 model turn 中不得并行调用多个 `write_todos`。
- 执行过程中可以修订 Todo 列表；新信息可能带来新事项，也可能使旧事项不再相关。

## 完成任务

全部工作完成后，必须在最后一次 `write_todos` 调用之后的下一条消息中给出最终答复，不能把最终答复放在
同一次 Tool 调用中。最终答复应直接从用户要求的实际结果开始，例如数据、计算、总结或分析，而不是只确认任务已完成。"""

_TASK_DESCRIPTION_ZH = """启动一个临时子 Agent，在隔离的上下文窗口中完成复杂、多步骤任务。

可用的子 Agent 类型及能力：
{available_agents}

调用要求：
- 独立任务应在同一条消息中发出多个 task 调用，以便并行执行。
- 除冻结运行时上下文和你已加载的 Skill state 外，每次调用都是隔离的；description 仍须包含完整任务上下文、
  已加载 Skill 中与该任务相关的约束、具体目标和期望输出。
- 子 Agent 的报告不会直接展示给用户；你需要综合结果并自行交付最终答复。
- 明确说明要创建内容、执行操作还是只做分析，不要假设子 Agent 能看到原始用户请求。"""

_GENERAL_PURPOSE_DESCRIPTION_ZH = (
    "通用执行 Agent；使用与主助理相同的业务能力，适合完成复杂、多步骤、上下文密集的任务。"
)


def _render_planning_system_prompt(skills: Sequence[SkillMetadata]) -> str:
    skill_lines = "\n".join(
        f"- {skill['name']}：{skill['description']}" for skill in skills
    )
    if not skill_lines:
        return ""
    return f"""## 专项指引

可按需读取的专项指引：
{skill_lines}

你必须先根据用户请求的语义，自主判断它是否匹配上述任一 Skill；用户无需知道、点名或要求使用 Skill。
只要请求匹配，就必须在提问澄清、调用 `write_todos`、创建 `task` 或直接回答之前，先调用 `load_skill` 阅读完整
说明。不得只根据这里的名称和简介直接执行，也不得等待用户指定 Skill。只加载与当前请求真正相关的 Skill。

加载后遵循 Skill 定义的澄清顺序和停止条件。首轮可以在一个简短问题中合并收集会阻塞下一步的最小硬约束；
交通、住宿、景点、餐饮等软偏好应一次只推进一个决策，每轮给出默认推荐，避免一次抛出完整问卷。把已加载
Skill 中影响执行的约束写入子 Agent 的完整 description。`load_skill` 只读取知识，不激活或授予业务工具。
规划协调器不能激活 Tool Profile；需要业务工具时，由执行子 Agent 在自己的 invocation 中按需激活。"""


def build_planning_agent(
    model: BaseChatModel,
    fast_agent: Any,
    *,
    model_call_limit: int = 12,
    context_window_tokens: int = 128_000,
    compaction_trigger_ratio: float = 0.75,
    compaction_target_ratio: float = 0.15,
    token_counter: Callable[[Iterable[MessageLikeRepresentation]], int] | None = None,
    skills_backend: BackendProtocol | None = None,
):
    """Build the native planning coordinator with an executable `task` Tool."""

    if model_call_limit < 1:
        raise ValueError("model call limit must be positive")
    if context_window_tokens < 100:
        raise ValueError("context window must contain at least 100 tokens")
    if not 0 < compaction_target_ratio < compaction_trigger_ratio <= 1:
        raise ValueError("compaction ratios must satisfy 0 < target < trigger <= 1")
    resolved_skills_backend = skills_backend or create_project_skills_backend(
        Path(__file__).resolve().parents[3] / "skills"
    )
    skills_middleware = create_project_skills_middleware(resolved_skills_backend)
    skill_index = load_project_skills_metadata(resolved_skills_backend)
    skill_tools = [
        create_load_skill_tool(
            backend=resolved_skills_backend,
            loaded_state_key="planner_loaded_skill_ids",
            reference_grants_state_key="planner_skill_reference_grants",
        ),
        create_load_skill_reference_tool(
            backend=resolved_skills_backend,
            reference_grants_state_key="planner_skill_reference_grants",
        ),
    ]

    worker: CompiledSubAgent = {
        "name": "general-purpose",
        "description": _GENERAL_PURPOSE_DESCRIPTION_ZH,
        "runnable": _selective_skill_state_worker(fast_agent),
    }
    summarization_options: dict[str, Any] = {
        "model": model,
        "trigger": (
            "tokens",
            max(1, int(context_window_tokens * compaction_trigger_ratio)),
        ),
        "keep": (
            "tokens",
            max(1, int(context_window_tokens * compaction_target_ratio)),
        ),
        "trim_tokens_to_summarize": None,
    }
    if token_counter is not None:
        summarization_options["token_counter"] = token_counter

    return create_agent(
        model=planning_supervisor_model_view(model),
        tools=skill_tools,
        system_prompt=_render_planning_system_prompt(skill_index),
        state_schema=PlanningAgentState,
        context_schema=AssistantRunContext,
        middleware=[
            skills_middleware,
            TodoListMiddleware(
                system_prompt=_WRITE_TODOS_SYSTEM_PROMPT_ZH,
                tool_description=_WRITE_TODOS_DESCRIPTION_ZH,
            ),
            SubAgentMiddleware(
                backend=StateBackend(),
                subagents=[worker],
                task_description=_TASK_DESCRIPTION_ZH,
            ),
            ModelCallLimitMiddleware(
                run_limit=model_call_limit,
                exit_behavior="end",
            ),
            PerToolCallLimitMiddleware.from_tools(
                skill_tools,
                default_run_limit=12,
            ),
            SummarizationMiddleware(**summarization_options),
            RuntimeContextMiddleware(),
            ToolProgressMiddleware(),
        ],
        name="AssistantPlanningAgent",
    )


def _selective_skill_state_worker(fast_agent: Any) -> RunnableLambda:
    """Map planner Skill knowledge into one worker without returning worker state."""

    def invoke(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
        result = fast_agent.invoke(_worker_input(state), config)
        return _worker_output(result)

    async def ainvoke(
        state: Mapping[str, Any],
        config: RunnableConfig,
    ) -> dict[str, Any]:
        result = await fast_agent.ainvoke(_worker_input(state), config)
        return _worker_output(result)

    return RunnableLambda(
        invoke,
        afunc=ainvoke,
        name="selective_skill_state_worker",
    )


def _worker_input(state: Mapping[str, Any]) -> dict[str, Any]:
    worker_state = dict(state)
    loaded_skill_ids = worker_state.pop("planner_loaded_skill_ids", None)
    reference_grants = worker_state.pop("planner_skill_reference_grants", None)
    if loaded_skill_ids:
        worker_state["loaded_skill_ids"] = list(loaded_skill_ids)
    if reference_grants:
        worker_state["skill_reference_grants"] = {
            skill_id: list(reference_ids)
            for skill_id, reference_ids in reference_grants.items()
        }
    return worker_state


def _worker_output(result: Mapping[str, Any]) -> dict[str, Any]:
    output = {"messages": result["messages"]}
    if result.get("structured_response") is not None:
        output["structured_response"] = result["structured_response"]
    return output


__all__ = ["build_planning_agent"]
