"""Intent to tool route mapping."""

from assistant_agent.runtime.planner import RuleBasedTaskPlanner
from assistant_agent.runtime.legacy_tool_mapping import (
    canonical_action_for_capability,
    canonical_tool_for_capability,
)
from assistant_agent.runtime.capability_models import canonical_intent
from assistant_agent.runtime.planning_models import IntentResult, TaskPlan, TaskStep
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.tools.models import ToolSelection
from assistant_agent.tools.ids import (
    IMAGE_GENERATION_TOOL_NAME,
    MEDIA_INSPECT_TOOL_NAME,
    SHOPPING_SEARCH_CAPABILITY,
    SHOPPING_SEARCH_TOOL_NAME,
)


class ToolRouter:
    """Build a task plan and tool selections from an intent."""

    def route(self, intent: IntentResult, request: UserRequest | None = None) -> TaskPlan:
        canonical = canonical_intent(intent.intent)
        if intent.intent == "ask_followup":
            return TaskPlan(
                goal="补充完成任务所需的信息",
                steps=[],
                requires_followup=True,
                followup_question="请补充你想让我处理的对象或目标。",
            )

        if canonical == "direct_chat":
            return TaskPlan(
                goal="直接回复用户",
                steps=[TaskStep(step_id="step_1", action="chat", tool_name=None)],
            )

        if canonical == "multi_step_orchestration":
            if request is not None:
                plan = RuleBasedTaskPlanner().plan(request)
                if plan.steps:
                    return plan
            return TaskPlan(
                goal="理解媒体中的商品，搜索相似款，比价并生成海报",
                steps=[
                    TaskStep(
                        step_id="step_1",
                        action="understand_video",
                        tool_name=MEDIA_INSPECT_TOOL_NAME,
                    ),
                    TaskStep(
                        step_id="step_2",
                        action=SHOPPING_SEARCH_CAPABILITY,
                        tool_name=SHOPPING_SEARCH_TOOL_NAME,
                        input_refs=["step_1"],
                        depends_on=["step_1"],
                    ),
                    TaskStep(
                        step_id="step_3",
                        action="generate_image",
                        tool_name=IMAGE_GENERATION_TOOL_NAME,
                        input_refs=["step_1", "step_2"],
                        depends_on=["step_2"],
                    ),
                ],
            )

        tool_name = canonical_tool_for_capability(canonical)
        action = canonical_action_for_capability(canonical) or canonical
        return TaskPlan(
            goal=intent.rationale,
            steps=[
                TaskStep(
                    step_id="step_1",
                    action=action,
                    tool_name=tool_name,
                )
            ],
        )

    def select_tools(self, intent: IntentResult, request: UserRequest | None = None) -> list[ToolSelection]:
        plan = self.route(intent, request)
        return [
            ToolSelection(
                tool_name=step.tool_name,
                reason=f"执行计划步骤：{step.action}",
                step_id=step.step_id,
            )
            for step in plan.steps
            if step.tool_name is not None
        ]
