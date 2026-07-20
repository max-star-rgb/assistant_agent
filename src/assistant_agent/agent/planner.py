"""Rule-based multi-step task planning."""

import re

from assistant_agent.schemas.planning import TaskPlan, TaskStep
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.services.tool_manifest import (
    SHOPPING_SEARCH_CAPABILITY,
    SHOPPING_SEARCH_TOOL_NAME,
)


class RuleBasedTaskPlanner:
    """Build a TaskPlan from request media and keyword rules."""

    url_re = re.compile(r"https?://\S+")
    memory_keywords = ("上次", "刚才", "之前", "以前", "我喜欢")
    memory_save_keywords = ("记住", "帮我记", "保存偏好")
    search_keywords = ("找", "搜索", "同款", "相似")
    web_search_keywords = (
        "最新",
        "最近",
        "实时",
        "新闻",
        "今天",
        "现在",
        "当前",
        "联网",
        "网上",
        "网页",
        "查一下",
        "查查",
        "latest",
        "recent",
        "current",
        "today",
        "now",
        "news",
        "online",
        "web",
        "look up",
    )
    explicit_web_search_keywords = ("联网搜索", "网上搜索", "网页搜索", "web search", "search the web", "internet search")
    web_fetch_keywords = (
        "打开",
        "读取",
        "读一下",
        "看一下",
        "正文",
        "网页内容",
        "fetch",
        "open",
        "read",
        "page content",
        "article",
    )
    product_hint_keywords = ("耳机", "鞋", "包", "衣服", "手机", "电脑", "椅", "桌", "灯", "相似款", "同款", "商品", "产品", "电商", "价格")
    compare_keywords = ("比价", "价格", "便宜", "比较")
    image_keywords = ("生成", "海报", "风格图", "换背景", "出图")
    render_keywords = ("渲染", "3d", "3D", "三维", "建模", "模型", "放到", "放进", "放入", "客厅", "展厅", "展示")
    render_target_spaces = ("客厅", "展厅", "办公室", "卧室", "空间", "商品展示", "展示空间")
    image_understanding_keywords = ("图里", "图片里", "照片里", "看图", "图中", "图上")
    video_understanding_keywords = ("视频", "总结这个视频", "总结这段视频", "视频里")
    vague_render_texts = {"渲染", "渲染一下", "看看效果", "做个展示", "展示一下", "3d", "3D"}

    def plan(self, request: UserRequest) -> TaskPlan:
        text = (request.text or "").strip()
        followup = self._missing_required_input_plan(text, request)
        if followup is not None:
            return followup

        steps: list[TaskStep] = []

        if self._contains(text, self.memory_keywords):
            self._append_step(
                steps,
                action="retrieve_memory",
                tool_name="memory_retrieval",
                required_inputs=["user_id", "session_id", "reference_phrase"],
                reason="用户引用历史上下文，先检索记忆。",
            )

        if request.image_ids or request.video_ids:
            is_video = bool(request.video_ids)
            self._append_step(
                steps,
                action="understand_video" if is_video else "understand_image",
                tool_name="video_understanding" if is_video else "vision_understanding",
                required_inputs=["video"] if is_video else ["image"],
                reason="用户提供了媒体输入，先理解媒体内容。",
            )

        if self._has_web_fetch_intent(text):
            self._append_step(
                steps,
                action="fetch_web",
                tool_name="web_fetch",
                required_inputs=["url"],
                reason="用户要求读取已知 URL 的网页正文。",
            )

        if self._has_web_search_intent(text) and not self._has_web_fetch_intent(text):
            self._append_step(
                steps,
                action="search_web",
                tool_name="web_search",
                required_inputs=["query"],
                reason="用户要求检索最新、实时或联网信息。",
            )

        if self._has_shopping_search_intent(text):
            self._append_step(
                steps,
                action=SHOPPING_SEARCH_CAPABILITY,
                tool_name=SHOPPING_SEARCH_TOOL_NAME,
                required_inputs=["query or visual_summary"],
                reason="用户要求查找商品，执行购物搜索。",
            )

        if self._contains(text, self.compare_keywords) and not self._has_step(steps, SHOPPING_SEARCH_TOOL_NAME):
            self._append_step(
                steps,
                action=SHOPPING_SEARCH_CAPABILITY,
                tool_name=SHOPPING_SEARCH_TOOL_NAME,
                required_inputs=["query"],
                reason="用户要求比价但没有候选商品，先执行购物搜索。",
            )

        if self._contains(text, self.image_keywords):
            self._append_step(
                steps,
                action="generate_image",
                tool_name="image_generation",
                required_inputs=["prompt"],
                reason="用户要求生成图片或海报。",
            )

        if self._has_render_intent(text):
            self._append_step(
                steps,
                action="render_3d",
                tool_name="render_3d",
                required_inputs=["scene_description"],
                reason="用户要求 3D 展示或渲染场景。",
            )

        if self._contains(text, self.memory_save_keywords):
            self._append_step(
                steps,
                action="save_memory",
                tool_name="memory_save",
                required_inputs=["content", "user_id"],
                reason="用户要求保存视频或偏好信息。",
            )

        return TaskPlan(goal=text or "执行多步任务", steps=steps)

    def _missing_required_input_plan(self, text: str, request: UserRequest) -> TaskPlan | None:
        if self._contains(text, self.image_understanding_keywords) and not request.image_ids and not request.video_ids:
            return TaskPlan(
                goal="补充图片输入",
                steps=[],
                requires_followup=True,
                followup_question="请补充要理解的图片，或说明你想处理的具体对象。",
            )
        if self._contains(text, self.video_understanding_keywords) and not request.video_ids:
            return TaskPlan(
                goal="补充视频输入",
                steps=[],
                requires_followup=True,
                followup_question="请补充要理解的视频，或说明你想处理的具体对象。",
            )
        if self._has_render_intent(text) and text in self.vague_render_texts:
            return TaskPlan(
                goal="补充渲染场景",
                steps=[],
                requires_followup=True,
                followup_question="请补充要渲染的场景，例如客厅、展厅、办公室或商品展示环境。",
            )
        return None

    def _append_step(
        self,
        steps: list[TaskStep],
        action: str,
        tool_name: str,
        required_inputs: list[str],
        reason: str,
        optional: bool = False,
    ) -> None:
        refs = self._previous_step_refs(steps)
        steps.append(
            TaskStep(
                step_id=self._step_id(len(steps)),
                action=action,
                tool_name=tool_name,
                input_refs=refs,
                depends_on=refs,
                required_inputs=required_inputs,
                optional=optional,
                reason=reason,
            )
        )

    def _has_step(self, steps: list[TaskStep], tool_name: str) -> bool:
        return any(step.tool_name == tool_name for step in steps)

    def _previous_step_refs(self, steps: list[TaskStep]) -> list[str]:
        if not steps:
            return []
        return [steps[-1].step_id]

    def _step_id(self, index: int) -> str:
        return f"step_{index + 1}"

    def _contains(self, text: str, keywords: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(keyword.lower() in lowered for keyword in keywords)

    def _has_shopping_search_intent(self, text: str) -> bool:
        if self._contains(
            text,
            (
                "购买",
                "买",
                "下单",
                "淘宝",
                "京东",
                "相似款",
                "同款",
                "商品链接",
                "shopping",
                "buy",
                "purchase",
            ),
        ):
            return True
        product_action_keywords = self.search_keywords + ("搜一下", "推荐", "recommend", "search", "find", "look for")
        return self._contains(text, product_action_keywords) and self._contains(text, self.product_hint_keywords)

    def _has_web_search_intent(self, text: str) -> bool:
        if self._contains(text, self.explicit_web_search_keywords):
            return True
        return self._contains(text, self.web_search_keywords) and not self._contains(text, self.product_hint_keywords)

    def _has_web_fetch_intent(self, text: str) -> bool:
        return bool(self.url_re.search(text)) and self._contains(
            text, self.web_fetch_keywords
        )

    def _has_render_intent(self, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        if any(keyword in text for keyword in ("3D", "三维", "渲染", "建模")) or "3d" in lowered:
            return True
        if "模型" in text and not any(phrase in text for phrase in ("模型识别", "模型判断", "语言模型")):
            return True
        if any(verb in text for verb in ("放到", "放进", "放入")) and any(
            space in text for space in self.render_target_spaces + ("场景",)
        ):
            return True
        if any(phrase in text for phrase in ("创建场景预览", "创建一个场景预览", "生成场景预览", "创建 3D 场景预览")):
            return True
        return False
