"""Rule-based intent detection."""

from dataclasses import dataclass
import re

from assistant_agent.agent.capability_validator import CapabilityValidator
from assistant_agent.schemas.capabilities import CapabilityName
from assistant_agent.schemas.intent_decision import IntentDecision, PlanStep
from assistant_agent.schemas.planning import IntentResult
from assistant_agent.schemas.requests import UserRequest


@dataclass(frozen=True)
class RuleMatch:
    """One deterministic rule match from the rule router."""

    rule_name: str
    intent: CapabilityName
    confidence: float
    reason: str


class IntentDetector:
    """Detect user intent with deterministic keyword rules."""

    url_re = re.compile(r"https?://\S+")
    memory_keywords = ("上次", "刚才", "之前", "以前", "我喜欢")
    save_memory_keywords = ("记住", "帮我记", "保存偏好")
    image_understanding_keywords = (
        "图里",
        "图片里",
        "图片中",
        "照片里",
        "看图",
        "图中",
        "图上",
        "图像",
        "画面",
        "是什么",
        "有什么",
        "描述",
        "分析",
    )
    video_understanding_keywords = ("视频", "发生了什么", "里面有什么", "总结这个视频", "总结这段视频")
    search_keywords = ("找", "找相似", "相似款", "同款", "找一下", "帮我找", "搜索")
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
    compare_keywords = ("比价", "比较价格", "哪个便宜", "便宜", "价格", "平台")
    generation_keywords = ("生成", "海报", "换背景", "风格图", "出图", "封面")
    render_keywords = ("客厅", "放到", "放进", "放入", "3d", "3D", "三维", "渲染", "建模", "模型", "看看效果")
    render_target_spaces = ("客厅", "展厅", "办公室", "卧室", "空间", "商品展示", "展示空间")
    media_reference_keywords = (
        "这张图",
        "这张图片",
        "这张照片",
        "图里",
        "图片里",
        "图片中",
        "照片里",
        "画面",
        "视频里",
        "这个视频",
        "这段视频",
    )
    vague_references = ("这个", "那个", "它")

    def __init__(self, validator: CapabilityValidator | None = None) -> None:
        self.validator = validator or CapabilityValidator()

    def detect_decision(self, request: UserRequest) -> IntentDecision:
        """Return a structured, validated rule-router decision."""

        text = (request.text or "").strip()
        matches = self._rule_matches(text, request)
        decision = self._decision_from_matches(matches, text, request)
        return self.validator.validate(decision, request)

    def detect(self, request: UserRequest) -> IntentResult:
        text = (request.text or "").strip()

        if not text and (request.image_ids or request.video_ids):
            return IntentResult(
                intent="ask_followup",
                confidence=0.55,
                missing_slots=["text"],
                rationale="用户提供了媒体输入，但没有说明希望执行什么任务。",
            )

        if self._is_multi_tool_task(text, request):
            return IntentResult(
                intent="multi_step_orchestration",
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

        if not request.video_ids and self._contains(text, self.video_understanding_keywords):
            return IntentResult(
                intent="ask_followup",
                confidence=0.7,
                missing_slots=["video"],
                rationale="用户请求理解视频，但当前请求没有视频输入。",
            )

        if (
            not request.image_ids
            and not request.video_ids
            and self._contains(text, self.image_understanding_keywords)
            and self._contains(text, self.media_reference_keywords + ("看图", "图中", "图上", "图像"))
            and not self._contains(text, self.generation_keywords)
        ):
            return IntentResult(
                intent="ask_followup",
                confidence=0.7,
                missing_slots=["image"],
                rationale="用户请求理解图片，但当前请求没有图片输入。",
            )

        if request.video_ids and self._contains(text, self.video_understanding_keywords):
            return IntentResult(
                intent="video_understanding",
                confidence=0.9,
                rationale="用户提供视频并询问视频内容。",
            )

        if request.image_ids and self._contains(text, self.image_understanding_keywords):
            return IntentResult(
                intent="image_understanding",
                confidence=0.9,
                rationale="用户提供图片并询问图片内容。",
            )

        if self._contains(text, self.compare_keywords):
            return IntentResult(
                intent="price_compare",
                confidence=0.85,
                rationale="用户询问价格、便宜程度或平台比较。",
            )

        if self._has_web_fetch_intent(text):
            return IntentResult(
                intent="web_fetch",
                confidence=0.85,
                rationale="用户要求读取已知 URL 的网页正文。",
            )

        if self._has_web_search_intent(text):
            return IntentResult(
                intent="web_search",
                confidence=0.85,
                rationale="用户要求检索最新、实时或联网信息。",
            )

        if self._has_product_search_intent(text):
            return IntentResult(
                intent="product_search",
                confidence=0.85,
                rationale="用户要求查找同款或相似商品。",
            )

        if self._has_render_intent(text):
            return IntentResult(
                intent="render_3d",
                confidence=0.85,
                rationale="用户要求放入场景、3D 渲染或查看效果。",
            )

        if self._contains(text, self.generation_keywords):
            return IntentResult(
                intent="image_generation",
                confidence=0.85,
                rationale="用户要求生成图片或海报。",
            )

        return IntentResult(
            intent="direct_chat",
            confidence=0.6,
            rationale="未命中特定工具意图，按普通对话处理。",
        )

    def _rule_matches(self, text: str, request: UserRequest) -> list[RuleMatch]:
        matches: list[RuleMatch] = []

        if not text and (request.image_ids or request.video_ids):
            return [
                RuleMatch(
                    rule_name="media_without_text",
                    intent="ask_followup",
                    confidence=0.55,
                    reason="用户提供了媒体输入，但没有说明希望执行什么任务。",
                )
            ]

        if self._needs_followup(text, request):
            return [
                RuleMatch(
                    rule_name="vague_reference",
                    intent="ask_followup",
                    confidence=0.55,
                    reason="用户使用了指代词，但当前请求缺少可解析上下文。",
                )
            ]

        if self._contains(text, self.save_memory_keywords):
            matches.append(
                RuleMatch("save_memory_keywords", "memory_save", 0.9, "用户明确要求保存偏好或信息。")
            )
        if self._contains(text, self.memory_keywords):
            matches.append(
                RuleMatch("memory_reference_keywords", "memory_retrieval", 0.9, "用户提到历史上下文，需要检索记忆。")
            )
        if self._contains(text, self.video_understanding_keywords):
            matches.append(
                RuleMatch("media_understanding_keywords", "video_understanding", 0.9, "用户询问视频内容。")
            )
        if self._contains(text, self.image_understanding_keywords) and self._contains(
            text, self.media_reference_keywords + ("看图", "图中", "图上", "图像")
        ):
            matches.append(
                RuleMatch("media_understanding_keywords", "image_understanding", 0.9, "用户询问图片内容。")
            )
        if self._has_web_fetch_intent(text):
            matches.append(
                RuleMatch("web_fetch_url_keywords", "web_fetch", 0.9, "用户要求读取已知 URL 的网页正文。")
            )
        if self._has_web_search_intent(text) and not self._has_web_fetch_intent(text):
            matches.append(
                RuleMatch("web_search_keywords", "web_search", 0.9, "用户要求检索最新、实时或联网信息。")
            )
        if self._has_product_search_intent(text):
            matches.append(
                RuleMatch("product_search_keywords", "product_search", 0.9, "用户要求查找同款或相似商品。")
            )
        if self._contains(text, self.compare_keywords):
            matches.append(
                RuleMatch("price_compare_keywords", "price_compare", 0.9, "用户询问价格、便宜程度或平台比较。")
            )
        if self._contains(text, self.generation_keywords) and not self._has_render_intent(text):
            matches.append(
                RuleMatch("generate_image_keywords", "image_generation", 0.95, "用户明确要求生成图片。")
            )
        if self._has_render_intent(text):
            matches.append(
                RuleMatch("render_scene_keywords", "render_3d", 0.9, "用户要求放入场景、3D 渲染或查看效果。")
            )

        if not matches:
            confidence = 0.45 if self._is_low_confidence_fallback(text) else 0.6
            matches.append(
                RuleMatch("fallback_direct_chat", "direct_chat", confidence, "未命中特定工具意图，按普通对话处理。")
            )
        return matches

    def _decision_from_matches(
        self,
        matches: list[RuleMatch],
        text: str,
        request: UserRequest,
    ) -> IntentDecision:
        matched_rules = [match.rule_name for match in matches]
        reason = "；".join(match.reason for match in matches)
        confidence = min(match.confidence for match in matches)
        capabilities = self._capabilities_from_matches(matches, text, request)
        primary_intent: CapabilityName = capabilities[0] if len(capabilities) == 1 else "multi_step_orchestration"
        if capabilities == ["ask_followup"]:
            primary_intent = "ask_followup"
        plan_steps = self._plan_steps_from_capabilities(capabilities)
        missing_inputs = self._missing_inputs_from_matches(matches)
        return IntentDecision(
            primary_intent=primary_intent,
            capabilities=capabilities,
            plan_steps=plan_steps,
            missing_inputs=missing_inputs,
            confidence=confidence,
            source="rule",
            reason=reason,
            matched_rules=matched_rules,
        )

    def _capabilities_from_matches(
        self,
        matches: list[RuleMatch],
        text: str,
        request: UserRequest,
    ) -> list[CapabilityName]:
        if any(match.intent == "ask_followup" for match in matches):
            return ["ask_followup"]

        ordered: list[CapabilityName] = []
        if any(match.intent == "memory_retrieval" for match in matches):
            ordered.append("memory_retrieval")
        if request.video_ids and any(match.intent == "video_understanding" for match in matches):
            ordered.append("video_understanding")
        elif request.image_ids and any(match.intent == "image_understanding" for match in matches):
            ordered.append("image_understanding")
        elif request.video_ids and self._contains(text, self.media_reference_keywords):
            ordered.append("video_understanding")
        elif request.image_ids and self._contains(text, self.media_reference_keywords):
            ordered.append("image_understanding")

        for capability in ("web_fetch", "web_search", "product_search", "price_compare", "image_generation", "render_3d", "memory_save"):
            if any(match.intent == capability for match in matches):
                ordered.append(capability)

        if not ordered:
            ordered.append(matches[0].intent)
        return self._dedupe(ordered)

    def _plan_steps_from_capabilities(self, capabilities: list[CapabilityName]) -> list[PlanStep]:
        if capabilities == ["ask_followup"]:
            return []
        return [
            PlanStep(
                step_id=f"step_{index + 1}",
                capability=capability,
                tool_name=self._tool_for_capability(capability),
                reason=f"规则路由命中能力：{capability}",
            )
            for index, capability in enumerate(capabilities)
        ]

    def _tool_for_capability(self, capability: CapabilityName) -> str | None:
        return {
            "direct_chat": None,
            "image_generation": "image_generation",
            "image_understanding": "vision_understanding",
            "video_understanding": "video_understanding",
            "web_search": "web_search",
            "web_fetch": "web_fetch",
            "product_search": "product_search",
            "price_compare": "price_compare",
            "render_3d": "render_3d",
            "memory_retrieval": "memory_retrieval",
            "memory_save": "memory_save",
            "multi_step_orchestration": None,
            "ask_followup": None,
        }[capability]

    def _missing_inputs_from_matches(self, matches: list[RuleMatch]) -> list[str]:
        missing_by_rule = {
            "media_without_text": ["text"],
            "vague_reference": ["context"],
        }
        missing: list[str] = []
        for match in matches:
            missing.extend(missing_by_rule.get(match.rule_name, []))
        return self._dedupe_strings(missing)

    def _dedupe(self, capabilities: list[CapabilityName]) -> list[CapabilityName]:
        deduped: list[CapabilityName] = []
        for capability in capabilities:
            if capability not in deduped:
                deduped.append(capability)
        return deduped

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped

    def _is_low_confidence_fallback(self, text: str) -> bool:
        if text in self.vague_references:
            return True
        return any(phrase in text for phrase in ("这个东西", "这个商品"))

    def _is_multi_tool_task(self, text: str, request: UserRequest) -> bool:
        groups = [
            self.video_understanding_keywords + self.image_understanding_keywords,
            self.memory_keywords + self.save_memory_keywords,
            self.search_keywords + self.web_search_keywords,
            self.compare_keywords,
            self.generation_keywords,
            self.render_keywords,
        ]
        matched_groups = sum(1 for keywords in groups if self._contains(text, keywords))
        has_sequence_marker = any(marker in text for marker in ("再", "然后", "并", "，", ","))
        if matched_groups >= 2 and has_sequence_marker:
            return True
        if self._contains(text, self.memory_keywords) and self._has_render_intent(text):
            return True

        has_media = bool(request.image_ids or request.video_ids)
        if not has_media:
            return False

        if self._has_product_search_intent(text) or self._has_web_search_intent(text) or self._contains(text, self.compare_keywords):
            return True
        if has_media and self._contains(text, self.generation_keywords) and self._contains(
            text, self.media_reference_keywords
        ):
            return True
        if request.video_ids and self._contains(text, self.save_memory_keywords) and self._contains(
            text,
            self.media_reference_keywords,
        ):
            return True
        if self._contains(text, self.memory_keywords) and self._contains(text, self.generation_keywords):
            return True
        if self._has_render_intent(text) and self._contains(text, self.media_reference_keywords):
            return True
        return False

    def _needs_followup(self, text: str, request: UserRequest) -> bool:
        if not text:
            return True
        return text in self.vague_references

    @staticmethod
    def _contains(text: str, keywords: tuple[str, ...]) -> bool:
        lowered = text.lower()
        return any(keyword.lower() in lowered for keyword in keywords)

    def _has_product_search_intent(self, text: str) -> bool:
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
