"""Rule-based multi-step task planning."""

from multimodal_agent.schemas.planning import TaskPlan, TaskStep
from multimodal_agent.schemas.requests import UserRequest


class RuleBasedTaskPlanner:
    """Build a TaskPlan from request media and keyword rules."""

    search_keywords = ("找", "搜索", "同款", "相似")
    compare_keywords = ("比价", "价格", "便宜", "比较")
    image_keywords = ("生成", "海报", "图片", "风格图")
    render_keywords = ("渲染", "3d", "3D", "放到场景", "放到客厅")

    def plan(self, request: UserRequest) -> TaskPlan:
        text = request.text or ""
        steps: list[TaskStep] = []

        if request.image_ids or request.video_ids:
            steps.append(
                TaskStep(
                    step_id=self._step_id(len(steps)),
                    action="understand_video" if request.video_ids else "understand_image",
                    tool_name="vision_understanding",
                )
            )

        if self._contains(text, self.search_keywords):
            steps.append(
                TaskStep(
                    step_id=self._step_id(len(steps)),
                    action="search_product",
                    tool_name="product_search",
                    input_refs=self._previous_step_refs(steps),
                    depends_on=self._previous_step_refs(steps),
                )
            )

        if self._contains(text, self.compare_keywords):
            steps.append(
                TaskStep(
                    step_id=self._step_id(len(steps)),
                    action="compare_price",
                    tool_name="price_compare",
                    input_refs=self._previous_step_refs(steps),
                    depends_on=self._previous_step_refs(steps),
                )
            )

        if self._contains(text, self.image_keywords):
            steps.append(
                TaskStep(
                    step_id=self._step_id(len(steps)),
                    action="generate_image",
                    tool_name="image_generation",
                    input_refs=self._previous_step_refs(steps),
                    depends_on=self._previous_step_refs(steps),
                )
            )

        if self._contains(text, self.render_keywords):
            steps.append(
                TaskStep(
                    step_id=self._step_id(len(steps)),
                    action="render_3d",
                    tool_name="render_3d",
                    input_refs=self._previous_step_refs(steps),
                    depends_on=self._previous_step_refs(steps),
                )
            )

        return TaskPlan(goal=text or "执行多步任务", steps=steps)

    def _previous_step_refs(self, steps: list[TaskStep]) -> list[str]:
        if not steps:
            return []
        return [steps[-1].step_id]

    def _step_id(self, index: int) -> str:
        return f"step_{index + 1}"

    def _contains(self, text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)
