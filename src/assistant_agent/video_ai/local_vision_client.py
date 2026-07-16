"""Local realtime video understanding protocol and deterministic mock client."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

from assistant_agent.video_ai.memory.state_manager import KeyframeMemoryRecord
from assistant_agent.video_ai.types import QueryAnswer, VideoFrame


REALTIME_VIDEO_PROMPT = """角色: 实时视觉理解器
简介:
语言: 中文
描述: 你只负责观察视频关键帧并生成结构化视觉事实，不承担主对话、工具选择、业务决策或用户回复。
技能:
1. 对连续图片按从左到右和时间顺序观察，先理解每张图的主体，再判断动作、物体和场景变化。
2. 仔细读取画面文字、品牌、商标、食品和产品线索，无法确认真实身份时标记不确定，不基于表面相似性假设。
3. 分析人物动作、物体运动方向、几何图形、图表、地图、视觉错觉和非英文字符时，区分图像线索、常识事实和可能偏差。
4. 当图中文字与常识或已知事实冲突时，保留观察到的文字并表达不确定，不替用户确认错误信息。
规则:
1. 只分析提供的视频关键帧；历史状态摘要仅作上下文参考，不能替代当前关键帧事实。
2. 不输出角色信息、解释性前言、Markdown、代码块或自然语言长答。
3. 只输出一个 json object，字段范围为: scene, objects, people, actions, changes_from_previous, important_events, summary。
4. 不调用工具，不提及主 LLM、系统提示、Provider、图片路径或内部实现。
5. 证据不足时使用空数组或简短不确定描述，不编造当前画面。
工作流程:
1. 先按顺序观察历史关键帧和当前关键帧。
2. 再比较新出现的人和物、位置变化、动作变化、场景变化和重要事件。
3. 最后输出结构化 json object。
初始化:
身为实时视觉理解器，必须遵守规则，并只返回结构化视觉事实。"""


class VisionObservation(BaseModel):
    """Structured keyframe understanding output."""

    scene: str = ""
    objects: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    changes_from_previous: str = ""
    important_events: list[str] = Field(default_factory=list)
    summary: str = ""
    provider: str = "mock"
    model: str = "mock-realtime-vision"
    errors: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int = 0


class VisionUnderstandingClient(Protocol):
    """Local client interface used by the selected-keyframe demo app."""

    def understand_keyframe(
        self,
        current_frame: VideoFrame,
        history_keyframes: list[KeyframeMemoryRecord],
        previous_state_summary: str,
    ) -> VisionObservation:
        """Understand a selected keyframe with recent keyframe context."""

    def answer_query(
        self,
        query: str,
        memory_state: dict[str, Any],
        recent_keyframes: list[KeyframeMemoryRecord],
    ) -> QueryAnswer:
        """Answer a user query using rolling memory and recent keyframes."""


class MockRealtimeVisionClient:
    """Deterministic local vision client for tests and offline demos."""

    def __init__(self) -> None:
        self.understand_calls = 0
        self.answer_calls = 0

    def understand_keyframe(
        self,
        current_frame: VideoFrame,
        history_keyframes: list[KeyframeMemoryRecord],
        previous_state_summary: str,
    ) -> VisionObservation:
        self.understand_calls += 1
        response = current_frame.metadata.get("vision_response") or current_frame.metadata.get("qwen_response")
        if isinstance(response, VisionObservation):
            return response.model_copy(update={"provider": "mock", "model": "mock-realtime-vision"})
        if isinstance(response, dict):
            return VisionObservation.model_validate(response).model_copy(
                update={"provider": "mock", "model": "mock-realtime-vision"}
            )

        label = str(current_frame.metadata.get("label") or current_frame.frame_id)
        summary = previous_state_summary or f"Observed frame {label}."
        return VisionObservation(
            scene=str(current_frame.metadata.get("scene") or ""),
            objects=_metadata_list(current_frame.metadata.get("objects")),
            people=_metadata_list(current_frame.metadata.get("people")),
            actions=_metadata_list(current_frame.metadata.get("actions")),
            changes_from_previous="" if history_keyframes else "Initial keyframe.",
            important_events=_metadata_list(current_frame.metadata.get("events")),
            summary=summary,
            provider="mock",
            model="mock-realtime-vision",
            latency_ms=1,
        )

    def answer_query(
        self,
        query: str,
        memory_state: dict[str, Any],
        recent_keyframes: list[KeyframeMemoryRecord],
    ) -> QueryAnswer:
        started_at = time.perf_counter()
        self.answer_calls += 1
        _ = recent_keyframes
        events = [event.get("event", "") for event in memory_state.get("events", []) if isinstance(event, dict)]
        current_state = str(memory_state.get("current_state") or "")
        if events:
            answer = f"Based on recent video memory: {'; '.join(events[-5:])}."
        elif current_state:
            answer = f"Based on current video memory: {current_state}"
        else:
            answer = "No recent video events are available in memory."
        if "cup" in query.lower() and "cup" not in answer.lower() and current_state:
            answer = f"{answer} Current state mentions: {current_state}"
        return QueryAnswer(
            answer=answer,
            memory_state=memory_state,
            qwen_called=True,
            latency_ms=int((time.perf_counter() - started_at) * 1000),
        )


def _keyframe_prompt(previous_state_summary: str, history_keyframes: list[KeyframeMemoryRecord]) -> str:
    history = [
        {
            "frame_id": record.frame_id,
            "timestamp_seconds": record.timestamp_seconds,
            "summary": record.summary,
            "scene": record.scene,
            "objects": list(record.objects),
            "people": list(record.people),
        }
        for record in history_keyframes
    ]
    return (
        f"{REALTIME_VIDEO_PROMPT}\n"
        f"上一轮状态摘要：{previous_state_summary or '无'}\n"
        f"最近历史关键帧：{json.dumps(history, ensure_ascii=False)}"
    )


def _metadata_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
