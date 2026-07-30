"""Finalization-phase transcript helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from assistant_agent.tools.observation import (
    PROVIDER_TOOL_CALL_ID_KEY,
    prompt_observation_payload,
)


FINALIZE_CONTINUATION_MESSAGE = (
    "工具阶段已结束。请根据以上原始请求和按执行顺序提供的工具结果，"
    "直接给出最终回答。不要调用任何工具。"
)


def is_runtime_only_observation(observation: Mapping[str, Any]) -> bool:
    """Return whether an observation is only an internal loop-guard diagnostic."""

    payload = prompt_observation_payload(observation)
    error = payload.get("error")
    return (
        payload.get("status") == "rejected"
        and isinstance(error, Mapping)
        and error.get("code")
        in {"duplicate_failed_tool_call", "duplicate_complete_tool_call"}
    )


def correlated_native_tool_pairs(
    native_calls: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """Join finalization observations to actual Provider calls, failing closed."""

    candidate_counts: dict[str, int] = {}
    for call in native_calls:
        for call_id in _native_call_candidate_ids(call):
            candidate_counts[call_id] = candidate_counts.get(call_id, 0) + 1

    calls_by_id: dict[str, dict[str, Any]] = {}
    call_names: dict[str, str] = {}
    ambiguous_call_ids: set[str] = set()
    for call in native_calls:
        identity = _consistent_native_call_identity(call)
        if identity is None:
            continue
        call_id, call_name = identity
        if candidate_counts.get(call_id) != 1:
            ambiguous_call_ids.add(call_id)
            continue
        if call_id in calls_by_id:
            ambiguous_call_ids.add(call_id)
            continue
        calls_by_id[call_id] = dict(call)
        call_names[call_id] = call_name
    for call_id in ambiguous_call_ids:
        calls_by_id.pop(call_id, None)
        call_names.pop(call_id, None)

    observation_counts: dict[str, int] = {}
    for observation in observations:
        if is_runtime_only_observation(observation):
            continue
        call_id = observation.get(PROVIDER_TOOL_CALL_ID_KEY)
        if isinstance(call_id, str) and call_id:
            observation_counts[call_id] = observation_counts.get(call_id, 0) + 1

    pairs: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for index, observation in enumerate(observations):
        if is_runtime_only_observation(observation):
            continue
        call_id = observation.get(PROVIDER_TOOL_CALL_ID_KEY)
        if (
            not isinstance(call_id, str)
            or not call_id
            or observation_counts.get(call_id) != 1
        ):
            continue
        call = calls_by_id.get(call_id)
        if call is None:
            continue
        observation_name = observation.get("tool_name")
        if (
            not isinstance(observation_name, str)
            or observation_name != call_names[call_id]
        ):
            continue
        pairs.append((index, call, dict(observation)))
    return pairs


def _native_call_candidate_ids(call: Mapping[str, Any]) -> set[str]:
    candidates: set[str] = set()
    normalized_id = call.get("id")
    if isinstance(normalized_id, str) and normalized_id:
        candidates.add(normalized_id)
    raw = call.get("raw")
    raw_id = raw.get("id") if isinstance(raw, Mapping) else None
    if isinstance(raw_id, str) and raw_id:
        candidates.add(raw_id)
    return candidates


def _consistent_native_call_identity(
    call: Mapping[str, Any],
) -> tuple[str, str] | None:
    raw = call.get("raw")
    raw_id = raw.get("id") if isinstance(raw, Mapping) else None
    normalized_id = call.get("id")
    if (
        isinstance(normalized_id, str)
        and normalized_id
        and isinstance(raw_id, str)
        and raw_id
        and normalized_id != raw_id
    ):
        return None
    call_id = normalized_id or raw_id
    if not isinstance(call_id, str) or not call_id:
        return None

    raw_function = raw.get("function") if isinstance(raw, Mapping) else None
    raw_name = (
        raw_function.get("name")
        if isinstance(raw_function, Mapping)
        else None
    )
    normalized_name = call.get("name")
    if (
        isinstance(normalized_name, str)
        and normalized_name
        and isinstance(raw_name, str)
        and raw_name
        and normalized_name != raw_name
    ):
        return None
    call_name = normalized_name or raw_name
    if not isinstance(call_name, str) or not call_name:
        return None
    return call_id, call_name


def finalize_fallback_text(observations: Sequence[Mapping[str, Any]]) -> str:
    """Build an honest deterministic reply after finalizer protocol failure."""

    payloads = [
        prompt_observation_payload(observation)
        for observation in observations
        if not is_runtime_only_observation(observation)
    ]
    failure_facts: list[str] = []
    for payload in payloads:
        if payload.get("status") == "succeeded":
            continue
        summary = payload.get("summary")
        error = payload.get("error")
        error_message = error.get("message") if isinstance(error, Mapping) else None
        fact = summary if isinstance(summary, str) and summary.strip() else error_message
        if isinstance(fact, str) and fact.strip() and fact.strip() not in failure_facts:
            failure_facts.append(fact.strip())

    has_success = any(payload.get("status") == "succeeded" for payload in payloads)
    if failure_facts:
        detail = "；".join(failure_facts[:2])
        if detail[-1] not in "。！？.!?":
            detail += "。"
        if has_success:
            return (
                f"本轮部分信息获取失败：{detail}"
                "虽然取得了其他工具结果，但现有证据仍不足以可靠完成你的请求。"
                "我不会据此编造结论，请稍后重试。"
            )
        return (
            f"本次工具未能返回足以完成请求的可靠结果：{detail}"
            "我不会据此编造结论，请稍后重试。"
        )
    if has_success:
        return (
            "我已经取得部分工具结果，但现有证据仍不足以可靠完成你的请求。"
            "我不会据此编造结论，请稍后重试。"
        )
    return (
        "本次工具未能返回足以完成请求的可靠结果。"
        "我不会据此编造结论，请稍后重试。"
    )
