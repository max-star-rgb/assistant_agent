"""Qwen-VL vision understanding client boundaries."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from assistant_agent.services.provider_errors import sanitize_error_message
from assistant_agent.services.real_vision_adapter import _json_object_from_text, image_to_data_url
from assistant_agent.video_ai.memory.state_manager import KeyframeMemoryRecord
from assistant_agent.video_ai.types import QueryAnswer, VideoFrame


REALTIME_VIDEO_PROMPT = """你是一个实时视频理解系统。

分析当前关键帧，并结合历史状态。

请只输出一个 json object，字段如下：

{
 "scene": "",
 "objects": [],
 "people": [],
 "actions": [],
 "changes_from_previous": "",
 "important_events": [],
 "summary": ""
}

重点关注：
- 新出现的人和物
- 物体位置变化
- 人的动作变化
- 场景变化
- 可能的重要事件
"""


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
    model: str = "mock-qwen-vl"
    errors: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int = 0


class VisionUnderstandingClient(Protocol):
    """Client interface used by the realtime observer."""

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


class MockQwenVisionClient:
    """Deterministic Qwen-like client for local tests and offline demos."""

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
        response = current_frame.metadata.get("qwen_response")
        if isinstance(response, VisionObservation):
            return response.model_copy(update={"provider": "mock", "model": "mock-qwen-vl"})
        if isinstance(response, dict):
            return VisionObservation.model_validate(response).model_copy(update={"provider": "mock", "model": "mock-qwen-vl"})

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
            model="mock-qwen-vl",
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


@dataclass(frozen=True)
class QwenVLConfig:
    """OpenAI-compatible Qwen-VL configuration."""

    api_key: str | None = None
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model: str = "qwen-vl-plus"
    timeout_seconds: float = 30.0


class QwenVLClient:
    """OpenAI-compatible Qwen-VL client.

    This client is not selected by default. Callers must explicitly configure it
    from an opt-in provider profile, consistent with the repository provider
    safety boundary.
    """

    provider = "qwen"

    def __init__(self, config: QwenVLConfig) -> None:
        self.config = config

    def understand_keyframe(
        self,
        current_frame: VideoFrame,
        history_keyframes: list[KeyframeMemoryRecord],
        previous_state_summary: str,
    ) -> VisionObservation:
        started_at = time.perf_counter()
        if not self.config.api_key:
            return _failed_observation(
                provider=self.provider,
                model=self.config.model,
                code="provider_unconfigured",
                message="qwen vision client requires QWEN_VISION_API_KEY or DASHSCOPE_API_KEY.",
                latency_ms=0,
            )
        try:
            content = _image_content(history_keyframes, current_frame)
            content.append(
                {
                    "type": "text",
                    "text": _keyframe_prompt(previous_state_summary, history_keyframes),
                }
            )
            data = self._chat(content, json_response=True)
            observation = VisionObservation.model_validate(_json_object_from_text(_message_text(data)))
            return observation.model_copy(
                update={
                    "provider": self.provider,
                    "model": self.config.model,
                    "latency_ms": int((time.perf_counter() - started_at) * 1000),
                }
            )
        except Exception as exc:
            return _failed_observation(
                provider=self.provider,
                model=self.config.model,
                code="provider_bad_response",
                message=sanitize_error_message(str(exc)),
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )

    def answer_query(
        self,
        query: str,
        memory_state: dict[str, Any],
        recent_keyframes: list[KeyframeMemoryRecord],
    ) -> QueryAnswer:
        started_at = time.perf_counter()
        if not self.config.api_key:
            return QueryAnswer(
                answer="provider_unconfigured: qwen vision client requires QWEN_VISION_API_KEY or DASHSCOPE_API_KEY.",
                memory_state=memory_state,
                qwen_called=False,
                latency_ms=0,
            )
        content = [
            *_keyframe_refs_content(recent_keyframes),
            {
                "type": "text",
                "text": (
                    "你是实时视频问答助手。不要重新扫描视频，只基于提供的 rolling memory 和最近关键帧回答。\n"
                    f"用户问题：{query}\n"
                    f"rolling memory JSON：{json.dumps(memory_state, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            data = self._chat(content, json_response=False)
            return QueryAnswer(
                answer=_message_text(data),
                memory_state=memory_state,
                qwen_called=True,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )
        except Exception as exc:
            return QueryAnswer(
                answer=f"provider_bad_response: {sanitize_error_message(str(exc))}",
                memory_state=memory_state,
                qwen_called=True,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
            )

    def _chat(self, content: list[dict[str, Any]], *, json_response: bool) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is required for qwen vision client") from exc
        client = OpenAI(api_key=self.config.api_key, base_url=self.config.base_url, timeout=self.config.timeout_seconds)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
        }
        if json_response:
            kwargs["response_format"] = {"type": "json_object"}
        return client.chat.completions.create(**kwargs)


def _keyframe_prompt(previous_state_summary: str, history_keyframes: list[KeyframeMemoryRecord]) -> str:
    history = [record.__dict__.copy() for record in history_keyframes]
    return (
        f"{REALTIME_VIDEO_PROMPT}\n"
        f"上一轮状态摘要：{previous_state_summary or '无'}\n"
        f"最近历史关键帧：{json.dumps(history, ensure_ascii=False)}"
    )


def _image_content(history_keyframes: list[KeyframeMemoryRecord], current_frame: VideoFrame) -> list[dict[str, Any]]:
    content = _keyframe_refs_content(history_keyframes)
    if current_frame.uri:
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(current_frame.uri)}})
    return content


def _keyframe_refs_content(keyframes: list[KeyframeMemoryRecord]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for record in keyframes:
        if record.uri:
            content.append({"type": "image_url", "image_url": {"url": image_to_data_url(record.uri)}})
    return content


def _message_text(data: Any) -> str:
    try:
        content = data.choices[0].message.content
    except Exception:
        content = None
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    if isinstance(content, str):
        return content
    if isinstance(data, dict):
        try:
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, str):
                return content
        except Exception:
            pass
    raise ValueError("missing provider message content")


def _failed_observation(*, provider: str, model: str, code: str, message: str, latency_ms: int) -> VisionObservation:
    return VisionObservation(
        summary=message,
        provider=provider,
        model=model,
        errors=[{"code": code, "message": message, "recoverable": True}],
        latency_ms=latency_ms,
    )


def _metadata_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
