"""Intent to tool route mapping."""

from assistant_agent.agent.planner import RuleBasedTaskPlanner
from assistant_agent.schemas.capabilities import canonical_intent
from assistant_agent.schemas.planning import IntentResult, TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSelection


class ToolRouter:
    """Build a task plan and tool selections from an intent."""

    tool_by_intent = {
        "image_understanding": "vision_understanding",
        "video_understanding": "video_understanding",
        "image_generation": "image_generation",
        "memory_retrieval": "memory_retrieval",
        "web_search": "web_search",
        "web_fetch": "web_fetch",
        "shopping_search": "shopping_search",
        "multi_step_orchestration": None,
        "direct_chat": None,
        "understand_image": "vision_understanding",
        "understand_video": "video_understanding",
        "search_web": "web_search",
        "fetch_web": "web_fetch",
        "read_url": "web_fetch",
        "generate_image": "image_generation",
        "render_3d": "render_3d",
        "retrieve_memory": "memory_retrieval",
        "save_memory": "memory_save",
    }

    action_by_intent = {
        "image_understanding": "understand_image",
        "video_understanding": "understand_video",
        "image_generation": "generate_image",
        "memory_retrieval": "retrieve_memory",
        "web_search": "search_web",
        "web_fetch": "fetch_web",
        "shopping_search": "shopping_search",
        "multi_step_orchestration": "multi_tool_task",
        "direct_chat": "chat",
        "understand_image": "understand_image",
        "understand_video": "understand_video",
        "search_web": "search_web",
        "fetch_web": "fetch_web",
        "read_url": "fetch_web",
        "generate_image": "generate_image",
        "render_3d": "render_3d",
        "retrieve_memory": "retrieve_memory",
        "save_memory": "save_memory",
    }

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
                        tool_name="video_understanding",
                    ),
                    TaskStep(
                        step_id="step_2",
                        action="shopping_search",
                        tool_name="shopping_search",
                        input_refs=["step_1"],
                        depends_on=["step_1"],
                    ),
                    TaskStep(
                        step_id="step_3",
                        action="generate_image",
                        tool_name="image_generation",
                        input_refs=["step_1", "step_2"],
                        depends_on=["step_2"],
                    ),
                ],
            )

        tool_name = self.tool_by_intent[intent.intent]
        return TaskPlan(
            goal=intent.rationale,
            steps=[
                TaskStep(
                    step_id="step_1",
                    action=self.action_by_intent[intent.intent],
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
