"""Intent to tool route mapping."""

from multimodal_agent.schemas.capabilities import canonical_intent
from multimodal_agent.schemas.planning import IntentResult, TaskPlan, TaskStep
from multimodal_agent.schemas.tools import ToolSelection


class ToolRouter:
    """Build a task plan and tool selections from an intent."""

    tool_by_intent = {
        "image_understanding": "vision_understanding",
        "video_understanding": "vision_understanding",
        "image_generation": "image_generation",
        "memory_retrieval": "memory_retrieval",
        "product_search": "product_search",
        "price_compare": "price_compare",
        "multi_step_orchestration": None,
        "direct_chat": None,
        "understand_image": "vision_understanding",
        "understand_video": "vision_understanding",
        "search_product": "product_search",
        "compare_price": "price_compare",
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
        "product_search": "search_product",
        "price_compare": "compare_price",
        "multi_step_orchestration": "multi_tool_task",
        "direct_chat": "chat",
        "understand_image": "understand_image",
        "understand_video": "understand_video",
        "search_product": "search_product",
        "compare_price": "compare_price",
        "generate_image": "generate_image",
        "render_3d": "render_3d",
        "retrieve_memory": "retrieve_memory",
        "save_memory": "save_memory",
    }

    def route(self, intent: IntentResult) -> TaskPlan:
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
            return TaskPlan(
                goal="理解媒体中的商品，搜索相似款，比价并生成海报",
                steps=[
                    TaskStep(
                        step_id="step_1",
                        action="understand_video",
                        tool_name="vision_understanding",
                    ),
                    TaskStep(
                        step_id="step_2",
                        action="search_product",
                        tool_name="product_search",
                        input_refs=["step_1"],
                        depends_on=["step_1"],
                    ),
                    TaskStep(
                        step_id="step_3",
                        action="compare_price",
                        tool_name="price_compare",
                        input_refs=["step_2"],
                        depends_on=["step_2"],
                    ),
                    TaskStep(
                        step_id="step_4",
                        action="generate_image",
                        tool_name="image_generation",
                        input_refs=["step_1", "step_2", "step_3"],
                        depends_on=["step_3"],
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

    def select_tools(self, intent: IntentResult) -> list[ToolSelection]:
        plan = self.route(intent)
        return [
            ToolSelection(
                tool_name=step.tool_name,
                reason=f"执行计划步骤：{step.action}",
                step_id=step.step_id,
            )
            for step in plan.steps
            if step.tool_name is not None
        ]
