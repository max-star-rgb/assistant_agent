"""Rule-based intent detection."""

from multimodal_agent.schemas.planning import IntentResult
from multimodal_agent.schemas.requests import UserRequest


class IntentDetector:
    """Detect user intent with deterministic keyword rules."""

    memory_keywords = ("上次", "刚才", "之前", "以前", "我喜欢")
    save_memory_keywords = ("记住", "帮我记", "保存偏好")
    image_understanding_keywords = ("图里", "图片", "照片", "看图", "是什么")
    video_understanding_keywords = ("视频", "发生了什么", "里面有什么")
    search_keywords = ("找相似", "相似款", "同款", "找一下", "帮我找", "搜索")
    compare_keywords = ("比价", "比较价格", "哪个便宜", "便宜", "价格", "平台")
    generation_keywords = ("生成", "海报", "换背景", "风格图", "出图")
    render_keywords = ("客厅", "放到", "场景", "3d", "3D", "渲染", "模型", "看看效果")
    vague_references = ("这个", "那个", "它")

    def detect(self, request: UserRequest) -> IntentResult:
        text = (request.text or "").strip()
        normalized = text.lower()

        if not text and (request.image_ids or request.video_ids):
            return IntentResult(
                intent="ask_followup",
                confidence=0.55,
                missing_slots=["text"],
                rationale="用户提供了媒体输入，但没有说明希望执行什么任务。",
            )

        if self._is_multi_tool_task(normalized):
            return IntentResult(
                intent="multi_tool_task",
                confidence=0.95,
                rationale="用户指令包含多个工具目标，需要规划多步骤任务。",
            )

        if self._contains(text, self.save_memory_keywords):
            return IntentResult(
                intent="save_memory",
                confidence=0.85,
                rationale="用户明确要求保存偏好或信息。",
            )

        if self._contains(text, self.memory_keywords):
            return IntentResult(
                intent="memory_retrieval",
                confidence=0.9,
                rationale="用户提到历史上下文，需要检索记忆。",
            )

        if self._needs_followup(text, request):
            return IntentResult(
                intent="ask_followup",
                confidence=0.7,
                missing_slots=["context"],
                rationale="用户使用了指代词，但当前请求缺少可解析上下文。",
            )

        if request.video_ids and self._contains(text, self.video_understanding_keywords):
            return IntentResult(
                intent="understand_video",
                confidence=0.9,
                rationale="用户提供视频并询问视频内容。",
            )

        if request.image_ids and self._contains(text, self.image_understanding_keywords):
            return IntentResult(
                intent="understand_image",
                confidence=0.9,
                rationale="用户提供图片并询问图片内容。",
            )

        if self._contains(text, self.compare_keywords):
            return IntentResult(
                intent="price_compare",
                confidence=0.85,
                rationale="用户询问价格、便宜程度或平台比较。",
            )

        if self._contains(text, self.search_keywords):
            return IntentResult(
                intent="product_search",
                confidence=0.85,
                rationale="用户要求查找同款或相似商品。",
            )

        if self._contains(text, self.generation_keywords):
            return IntentResult(
                intent="image_generation",
                confidence=0.85,
                rationale="用户要求生成图片或海报。",
            )

        if self._contains(text, self.render_keywords):
            return IntentResult(
                intent="render_3d",
                confidence=0.85,
                rationale="用户要求放入场景、3D 渲染或查看效果。",
            )

        if request.video_ids:
            return IntentResult(
                intent="understand_video",
                confidence=0.65,
                rationale="请求包含视频输入，默认先理解视频内容。",
            )

        if request.image_ids:
            return IntentResult(
                intent="understand_image",
                confidence=0.65,
                rationale="请求包含图片输入，默认先理解图片内容。",
            )

        return IntentResult(
            intent="direct_chat",
            confidence=0.6,
            rationale="未命中特定工具意图，按普通对话处理。",
        )

    def _is_multi_tool_task(self, text: str) -> bool:
        groups = [
            self.video_understanding_keywords + self.image_understanding_keywords,
            self.search_keywords,
            self.compare_keywords,
            self.generation_keywords,
            self.render_keywords,
        ]
        matched_groups = sum(1 for keywords in groups if self._contains(text, keywords))
        has_sequence_marker = any(marker in text for marker in ("再", "然后", "并", "，", ","))
        return matched_groups >= 2 and has_sequence_marker

    def _needs_followup(self, text: str, request: UserRequest) -> bool:
        if not text:
            return True
        return text in self.vague_references and not request.image_ids and not request.video_ids

    @staticmethod
    def _contains(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)
