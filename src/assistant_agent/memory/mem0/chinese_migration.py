"""Operator-only migration of one user's Mem0 memories to Simplified Chinese."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from collections import Counter
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.identity import RequestIdentity
from assistant_agent.memory.mem0.identity import bind_mem0_identity
from assistant_agent.memory.mem0.transport import (
    Mem0HttpRequest,
    Mem0OperationError,
    Mem0Transport,
)


_CHINESE_TEXT_RE = re.compile(r"[\u3400-\u9fff]")
_URL_RE = re.compile(r"https?://[^\s)\]}>\"']+")
_CHINESE_URL_TRAILING_PUNCTUATION = "。，；：！？）》】」』"
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*(?:%|万)?")
_MIN_UPDATE_TIMEOUT_SECONDS = 30.0
_ENGLISH_MONTH_NUMBERS = {
    month: str(index)
    for index, month in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


class ChineseMemoryMigrationReport(BaseModel):
    """Content-free result of inspecting or applying one scoped migration."""

    total: int = Field(ge=0)
    candidates: int = Field(ge=0)
    already_chinese: int = Field(ge=0)
    updated: int = Field(ge=0)
    updated_memory_ids: list[str] = Field(default_factory=list)
    failed_memory_id: str | None = None
    error_code: str | None = None


def migrate_memories_to_chinese(
    *,
    identity: RequestIdentity,
    identity_namespace: str,
    transport: Mem0Transport,
    translate: Callable[[str], str],
    apply: bool,
    timeout_seconds: float = 5.0,
    limit: int = 50,
) -> ChineseMemoryMigrationReport:
    """Inspect or migrate the bounded Mem0 scope for one trusted identity."""

    engine_identity = bind_mem0_identity(
        identity,
        namespace=identity_namespace,
    )
    payload = _request(
        transport,
        Mem0HttpRequest(
            method="GET",
            path="/memories",
            query={
                **engine_identity.long_term_filters,
                "limit": str(limit),
            },
            timeout_seconds=timeout_seconds,
        ),
    )
    memories = _memory_items(payload.get("results"))
    candidates = [
        item
        for item in memories
        if not _CHINESE_TEXT_RE.search(_memory_text(item))
    ]
    report = ChineseMemoryMigrationReport(
        total=len(memories),
        candidates=len(candidates),
        already_chinese=len(memories) - len(candidates),
        updated=0,
    )
    if not apply:
        return report
    updated_ids: list[str] = []
    for item in candidates:
        memory_id = str(item.get("id") or item.get("memory_id") or "")
        original = _memory_text(item)
        if not memory_id:
            return _failure(
                report,
                updated_ids=updated_ids,
                error_code="memory_id_missing",
            )
        try:
            translated = translate(original).strip()
        except Exception:
            return _failure(
                report,
                updated_ids=updated_ids,
                memory_id=memory_id,
                error_code="memory_translation_failed",
            )
        translation_error = _translation_error(original, translated)
        if translation_error:
            return _failure(
                report,
                updated_ids=updated_ids,
                memory_id=memory_id,
                error_code=translation_error,
            )
        try:
            _request(
                transport,
                Mem0HttpRequest(
                    method="PUT",
                    path=f"/memories/{memory_id}",
                    body={"memory": translated},
                    timeout_seconds=max(
                        timeout_seconds,
                        _MIN_UPDATE_TIMEOUT_SECONDS,
                    ),
                ),
            )
            current = _request(
                transport,
                Mem0HttpRequest(
                    method="GET",
                    path=f"/memories/{memory_id}",
                    timeout_seconds=timeout_seconds,
                ),
            )
            if _memory_text(current) != translated:
                return _failure(
                    report,
                    updated_ids=updated_ids,
                    memory_id=memory_id,
                    error_code="memory_update_verification_failed",
                )
            history = _request(
                transport,
                Mem0HttpRequest(
                    method="GET",
                    path=f"/memories/{memory_id}/history",
                    timeout_seconds=timeout_seconds,
                ),
            )
            if not _history_preserves_update(
                history.get("history"),
                original=original,
                translated=translated,
            ):
                return _failure(
                    report,
                    updated_ids=updated_ids,
                    memory_id=memory_id,
                    error_code="memory_history_verification_failed",
                )
        except Exception:
            return _failure(
                report,
                updated_ids=updated_ids,
                memory_id=memory_id,
                error_code="memory_update_failed",
            )
        updated_ids.append(memory_id)
    return report.model_copy(
        update={
            "updated": len(updated_ids),
            "updated_memory_ids": updated_ids,
        }
    )


def _request(
    transport: Mem0Transport,
    request: Mem0HttpRequest,
) -> Mapping[str, Any]:
    payload = transport(request)
    if not isinstance(payload, Mapping):
        raise Mem0OperationError(
            request.path,
            "Mem0 returned an invalid migration response",
        )
    return payload


def _memory_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _memory_text(item: Mapping[str, Any]) -> str:
    return str(item.get("memory") or item.get("text") or "")


def _translation_error(original: str, translated: str) -> str | None:
    if not translated or not _CHINESE_TEXT_RE.search(translated):
        return "memory_translation_not_chinese"
    if _url_tokens(original) != _url_tokens(translated):
        return "memory_translation_url_mismatch"
    if _semantic_number_tokens(original) != _semantic_number_tokens(
        translated
    ):
        return "memory_translation_number_mismatch"
    return None


def _url_tokens(text: str) -> Counter[str]:
    return Counter(
        url.rstrip(_CHINESE_URL_TRAILING_PUNCTUATION)
        for url in _URL_RE.findall(text)
    )


def _semantic_number_tokens(text: str) -> Counter[str]:
    tokens = Counter(
        _normalized_number_token(token)
        for token in _NUMBER_RE.findall(text)
    )
    normalized = text.casefold()
    for month, number in _ENGLISH_MONTH_NUMBERS.items():
        tokens[number] += len(
            re.findall(rf"\b{re.escape(month)}\b", normalized)
        )
    return tokens


def _normalized_number_token(token: str) -> str:
    suffix = "%" if token.endswith("%") else ""
    has_ten_thousands = token.endswith("万")
    numeric = token.removesuffix("%").removesuffix("万").replace(",", "")
    value = Decimal(numeric)
    if has_ten_thousands:
        value *= Decimal("10000")
    normalized = format(value.normalize(), "f")
    return f"{normalized}{suffix}"


def _history_preserves_update(
    value: Any,
    *,
    original: str,
    translated: str,
) -> bool:
    if not isinstance(value, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("old_memory") == original
        and item.get("new_memory") == translated
        for item in value
    )


def _failure(
    report: ChineseMemoryMigrationReport,
    *,
    updated_ids: list[str],
    error_code: str,
    memory_id: str | None = None,
) -> ChineseMemoryMigrationReport:
    return report.model_copy(
        update={
            "updated": len(updated_ids),
            "updated_memory_ids": list(updated_ids),
            "failed_memory_id": memory_id,
            "error_code": error_code,
        }
    )
