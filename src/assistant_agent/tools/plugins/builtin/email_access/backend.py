"""Plugin-private offline email backend.

Real MCP email capabilities are exposed directly as official LangChain tools by
the native inventory; this module only supports deterministic mock mode.
"""

from __future__ import annotations

from typing import Protocol

from assistant_agent.tools.plugins.builtin.email_access.models import (
    EmailReadRequest,
    EmailReadResult,
    EmailSearchMatch,
    EmailSearchRequest,
    EmailSearchResult,
)

EMAIL_SEARCH_TOOL_NAME = "email_search"
EMAIL_READ_TOOL_NAME = "email_read"


class EmailBackend(Protocol):
    def search(self, request: EmailSearchRequest) -> EmailSearchResult: ...

    def read(self, request: EmailReadRequest) -> EmailReadResult: ...


class MockEmailBackend:
    """Deterministic offline mailbox used only in mock mode."""

    provider = "mock"

    def search(self, request: EmailSearchRequest) -> EmailSearchResult:
        matches = [
            EmailSearchMatch(
                message_id="mock-email-1",
                thread_id="mock-thread-1",
            )
        ]
        return EmailSearchResult(
            success=True,
            query_used=request.query,
            matches=matches[: request.limit],
            summary="Mock mailbox search returned 1 message.",
            provider=self.provider,
            latency_ms=1,
            output_ref="mock://email/search",
        )

    def read(self, request: EmailReadRequest) -> EmailReadResult:
        content = "\n\n---\n\n".join(
            (
                f"Message ID: {message_id}\n"
                "Subject: Mock project update\n"
                "From: sender@example.com\n"
                "Date: 2026-07-25T09:00:00+08:00\n\n"
                "--- BODY ---\n"
                "The project is on schedule. Please review the attached milestones."
            )
            for message_id in request.message_ids
        )
        return _bounded_read_result(
            message_ids=request.message_ids,
            content=content,
            max_total_chars=request.max_total_chars,
            provider=self.provider,
            output_ref="mock://email/read",
            latency_ms=1,
        )


def _bounded_read_result(
    *,
    message_ids: list[str],
    content: str,
    max_total_chars: int,
    provider: str,
    output_ref: str,
    latency_ms: int,
) -> EmailReadResult:
    original_chars = len(content)
    bounded = content[:max_total_chars]
    truncated = len(bounded) < original_chars
    if truncated:
        bounded += "\n\n[邮件内容已按本地上下文上限截断]"
    return EmailReadResult(
        success=True,
        message_ids=message_ids,
        content=bounded,
        original_chars=original_chars,
        truncated=truncated,
        summary=f"Read {len(message_ids)} email message(s) as untrusted evidence.",
        provider=provider,
        latency_ms=latency_ms,
        output_ref=output_ref,
    )
