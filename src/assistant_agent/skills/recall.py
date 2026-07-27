"""Deterministic recall for prompt-safe skill capability descriptors."""

from __future__ import annotations

from dataclasses import dataclass
import re

from assistant_agent.context.models import ToolCapabilityDescriptor
from assistant_agent.runtime.requests import UserRequest


@dataclass(frozen=True)
class SkillRecallSelection:
    """Skill descriptors recalled from the current request text."""

    candidate_skill_ids: list[str]
    reasons_by_skill: dict[str, list[str]]


_ASCII_STOPWORDS = {
    "and",
    "ask",
    "asks",
    "backed",
    "for",
    "from",
    "guidance",
    "into",
    "look",
    "needs",
    "please",
    "the",
    "this",
    "through",
    "use",
    "user",
    "when",
    "with",
}


def recall_skill_descriptors(
    request: UserRequest,
    descriptors: list[ToolCapabilityDescriptor],
) -> SkillRecallSelection:
    """Return descriptors whose prompt-safe usage text matches the request."""

    request_tokens = _request_tokens(request.text)
    if not request_tokens:
        return SkillRecallSelection(candidate_skill_ids=[], reasons_by_skill={})

    candidate_ids: list[str] = []
    reasons: dict[str, list[str]] = {}
    for descriptor in descriptors:
        descriptor_tokens = _descriptor_tokens(descriptor)
        matches = sorted(request_tokens.intersection(descriptor_tokens))
        if not matches:
            continue
        candidate_ids.append(descriptor.name)
        reasons[descriptor.name] = [f"matched_token:{token}" for token in matches[:8]]
    return SkillRecallSelection(candidate_skill_ids=candidate_ids, reasons_by_skill=reasons)


def _request_tokens(text: str) -> set[str]:
    return _expand_request_tokens(_tokens(text), text)


def _descriptor_tokens(descriptor: ToolCapabilityDescriptor) -> set[str]:
    values = [
        descriptor.name.replace("_", " "),
        descriptor.description,
        *descriptor.when_to_use,
        *descriptor.safe_examples,
    ]
    return _tokens(" ".join(value for value in values if value))


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", lowered)
        if len(token) >= 3 and token not in _ASCII_STOPWORDS
    }
    for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(sequence) == 1:
            tokens.add(sequence)
            continue
        for width in (2, 3, 4):
            if len(sequence) < width:
                continue
            tokens.update(
                sequence[index : index + width]
                for index in range(0, len(sequence) - width + 1)
            )
    return tokens


def _expand_request_tokens(tokens: set[str], text: str) -> set[str]:
    expanded = set(tokens)
    lowered = text.lower()
    if any(term in lowered for term in ("今天", "今日", "现在", "当前", "recent")):
        expanded.update({"today", "current", "latest"})
    if "最新" in lowered:
        expanded.update({"latest", "current"})
    if any(term in lowered for term in ("新闻", "消息", "资讯", "头条")):
        expanded.update({"news", "headlines", "information"})
    if any(term in lowered for term in ("联网", "网上", "搜索", "查一下", "查找", "查询")):
        expanded.update({"web", "search", "lookup"})
    return expanded
