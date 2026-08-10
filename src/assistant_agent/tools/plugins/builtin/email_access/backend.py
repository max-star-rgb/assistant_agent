"""Plugin-private mock and Workspace MCP email backends."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from assistant_agent.mcp.adapter import MCPToolRunner, namespaced_mcp_tool_name
from assistant_agent.mcp.config import MCPServerConfig
from assistant_agent.tools.plugins.builtin.email_access.models import (
    EmailProviderError,
    EmailReadRequest,
    EmailReadResult,
    EmailSearchMatch,
    EmailSearchRequest,
    EmailSearchResult,
)
from assistant_agent.tools.models import ToolResult
from assistant_agent.providers.provider_errors import (
    is_recoverable_provider_error,
    normalize_provider_error_code,
    sanitize_error_message,
)


EMAIL_SEARCH_TOOL_NAME = "email_search"
EMAIL_READ_TOOL_NAME = "email_read"
WORKSPACE_EMAIL_PROFILE = "workspace_mcp_v1"
_MESSAGE_PAIR_PATTERN = re.compile(
    r"Message ID:\s*(?P<message_id>[^\s]+).*?"
    r"Thread ID:\s*(?P<thread_id>[^\s]+)",
    re.DOTALL,
)
_NEXT_PAGE_PATTERN = re.compile(r"page_token='(?P<token>[^']+)'")


class EmailBackend(Protocol):
    """Provider-neutral boundary owned by the email Plugin."""

    def search(self, request: EmailSearchRequest) -> EmailSearchResult:
        """Search message identifiers and thread identifiers."""

    def read(self, request: EmailReadRequest) -> EmailReadResult:
        """Read bounded message content as untrusted evidence."""


@dataclass(frozen=True)
class EmailMCPBinding:
    """One stable email capability mapped to one remote MCP tool."""

    server_name: str
    tool_name: str
    namespaced_tool_name: str
    profile: str
    user_email: str | None

    @property
    def provider(self) -> str:
        return f"mcp:{self.server_name}.{self.tool_name}"

    @property
    def output_ref(self) -> str:
        return f"mcp://{self.server_name}/{self.tool_name}"


class MockEmailBackend:
    """Deterministic offline mailbox used in mock mode."""

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


class WorkspaceMCPEmailBackend:
    """Workspace MCP implementation kept private to the email Plugin."""

    def __init__(
        self,
        *,
        runner: MCPToolRunner,
        search_binding: EmailMCPBinding | None,
        read_binding: EmailMCPBinding | None,
    ) -> None:
        self.runner = runner
        self.search_binding = search_binding
        self.read_binding = read_binding

    def search(self, request: EmailSearchRequest) -> EmailSearchResult:
        binding = self.search_binding
        if binding is None:
            return _failed_search(
                request,
                provider="mcp",
                output_ref="unconfigured://email/search",
                code="provider_unconfigured",
                message="Email search MCP mapping is not configured.",
            )
        started = time.monotonic()
        result = _run_mcp_tool(
            self.runner,
            binding,
            _search_tool_input(request, binding),
        )
        latency_ms = _latency_ms(started)
        if not result.success:
            return _failed_search(
                request,
                provider=binding.provider,
                output_ref=result.output_ref or binding.output_ref,
                code=_mcp_failure_code(result),
                message=result.error or "Email search MCP call failed.",
                latency_ms=latency_ms,
            )
        content = _tool_result_text(result)
        matches = _search_matches(content)
        if not matches and not _is_empty_search_result(content):
            return _failed_search(
                request,
                provider=binding.provider,
                output_ref=result.output_ref or binding.output_ref,
                code="provider_bad_response",
                message="Email search response did not contain message identifiers.",
                latency_ms=latency_ms,
            )
        return EmailSearchResult(
            success=True,
            query_used=request.query,
            matches=matches[: request.limit],
            next_page_token=_next_page_token(content),
            summary=f"Email search returned {len(matches[: request.limit])} message(s).",
            provider=binding.provider,
            latency_ms=latency_ms,
            output_ref=result.output_ref or binding.output_ref,
        )

    def read(self, request: EmailReadRequest) -> EmailReadResult:
        binding = self.read_binding
        if binding is None:
            return _failed_read(
                request,
                provider="mcp",
                output_ref="unconfigured://email/read",
                code="provider_unconfigured",
                message="Email read MCP mapping is not configured.",
            )
        started = time.monotonic()
        result = _run_mcp_tool(
            self.runner,
            binding,
            _read_tool_input(request, binding),
        )
        latency_ms = _latency_ms(started)
        if not result.success:
            return _failed_read(
                request,
                provider=binding.provider,
                output_ref=result.output_ref or binding.output_ref,
                code=_mcp_failure_code(result),
                message=result.error or "Email read MCP call failed.",
                latency_ms=latency_ms,
            )
        content = _tool_result_text(result)
        if not content.strip():
            return _failed_read(
                request,
                provider=binding.provider,
                output_ref=result.output_ref or binding.output_ref,
                code="provider_bad_response",
                message="Email read response did not contain message content.",
                latency_ms=latency_ms,
            )
        return _bounded_read_result(
            message_ids=request.message_ids,
            content=content,
            max_total_chars=request.max_total_chars,
            provider=binding.provider,
            output_ref=result.output_ref or binding.output_ref,
            latency_ms=latency_ms,
        )


def configured_email_bindings(
    server_configs: list[MCPServerConfig],
) -> dict[str, EmailMCPBinding]:
    """Resolve first explicit mapping for each stable email Tool."""

    bindings: dict[str, EmailMCPBinding] = {}
    for server in server_configs:
        mapping = server.email_tools
        adapter_config = server.adapter_config()
        for capability, tool_name in (
            (EMAIL_SEARCH_TOOL_NAME, mapping.search),
            (EMAIL_READ_TOOL_NAME, mapping.read_batch),
        ):
            if not tool_name or capability in bindings:
                continue
            bindings[capability] = EmailMCPBinding(
                server_name=server.server_name,
                tool_name=tool_name,
                namespaced_tool_name=namespaced_mcp_tool_name(
                    adapter_config,
                    tool_name,
                ),
                profile=mapping.profile,
                user_email=mapping.user_email,
            )
    return bindings


def create_mcp_runner(
    server_configs: list[MCPServerConfig],
) -> MCPToolRunner | None:
    """Create the standard runner without moving Plugin code into services."""

    if not server_configs:
        return None
    try:
        from assistant_agent.mcp.sdk_client import SdkMCPClientRunner

        return SdkMCPClientRunner(server_configs)
    except ImportError:
        from assistant_agent.mcp.stdio_client import StdioMCPClientRunner

        return StdioMCPClientRunner(server_configs)


def _search_tool_input(
    request: EmailSearchRequest,
    binding: EmailMCPBinding,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": request.query,
        "page_size": request.limit,
    }
    if request.page_token:
        payload["page_token"] = request.page_token
    if binding.profile == WORKSPACE_EMAIL_PROFILE:
        payload["user_google_email"] = binding.user_email
    return payload


def _read_tool_input(
    request: EmailReadRequest,
    binding: EmailMCPBinding,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message_ids": request.message_ids,
    }
    if binding.profile == WORKSPACE_EMAIL_PROFILE:
        payload.update(
            {
                "user_google_email": binding.user_email,
                "format": "full",
                "body_format": "text",
            }
        )
    return payload


def _run_mcp_tool(
    runner: MCPToolRunner,
    binding: EmailMCPBinding,
    tool_input: dict[str, Any],
) -> ToolResult:
    try:
        return runner.run_tool(
            server_name=binding.server_name,
            tool_name=binding.tool_name,
            tool_input=tool_input,
        )
    except Exception as exc:  # pragma: no cover - defensive provider boundary
        return ToolResult(
            tool_name=binding.namespaced_tool_name,
            success=False,
            error=sanitize_error_message(exc),
            output_ref=binding.output_ref,
        )


def _mcp_failure_code(result: ToolResult) -> str:
    structured_code = _mcp_structured_failure_code(result)
    if structured_code is not None:
        return structured_code
    message = (result.error or "").strip()
    lowered = message.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "provider_timeout"
    if _is_google_auth_failure(lowered):
        return "provider_auth_failed"
    prefix = message.split(":", maxsplit=1)[0]
    normalized = normalize_provider_error_code(prefix)
    if normalized.startswith("provider_") and normalized != "provider_unknown_error":
        return normalized
    return "provider_execution_failed"


def _mcp_structured_failure_code(result: ToolResult) -> str | None:
    candidates: list[object] = []
    if result.contract is not None:
        candidates.extend(error.code for error in result.contract.errors)
    for payload in (result.model_observation, result.data):
        if not isinstance(payload, dict):
            continue
        candidates.extend(_error_codes(payload.get("errors")))
        structured = payload.get("structured_content")
        if isinstance(structured, dict):
            candidates.extend(_error_codes(structured.get("errors")))
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        normalized = normalize_provider_error_code(candidate)
        if normalized.startswith("provider_") and normalized != "provider_unknown_error":
            return normalized
    return None


def _error_codes(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return [
        item.get("code")
        for item in value
        if isinstance(item, dict)
    ]


def _is_google_auth_failure(message: str) -> bool:
    return any(
        marker in message
        for marker in (
            "google authentication needed",
            "google oauth authorization",
            "authorize google gmail",
        )
    )


def _tool_result_text(result: ToolResult) -> str:
    observation = result.model_observation
    if isinstance(observation, dict):
        for key in ("content", "text"):
            value = observation.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    data = result.data
    if isinstance(data, dict):
        structured = data.get("structured_content")
        if isinstance(structured, dict):
            for key in ("content", "text", "summary"):
                value = structured.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        content = data.get("content")
        if isinstance(content, list):
            texts = [
                item.get("text", "").strip()
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
                and item.get("text", "").strip()
            ]
            if texts:
                return "\n".join(texts)
    if isinstance(observation, dict):
        summary = observation.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return ""


def _search_matches(content: str) -> list[EmailSearchMatch]:
    matches: list[EmailSearchMatch] = []
    seen: set[str] = set()
    for match in _MESSAGE_PAIR_PATTERN.finditer(content):
        message_id = match.group("message_id")
        if message_id == "unknown" or message_id in seen:
            continue
        seen.add(message_id)
        thread_id = match.group("thread_id")
        matches.append(
            EmailSearchMatch(
                message_id=message_id,
                thread_id=None if thread_id == "unknown" else thread_id,
            )
        )
    return matches


def _next_page_token(content: str) -> str | None:
    match = _NEXT_PAGE_PATTERN.search(content)
    return match.group("token") if match else None


def _is_empty_search_result(content: str) -> bool:
    return "no messages found" in content.lower()


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


def _failed_search(
    request: EmailSearchRequest,
    *,
    provider: str,
    output_ref: str,
    code: str,
    message: str,
    latency_ms: int = 0,
) -> EmailSearchResult:
    error = _error(code, message)
    return EmailSearchResult(
        success=False,
        query_used=request.query,
        summary=error.message,
        provider=provider,
        latency_ms=latency_ms,
        output_ref=output_ref,
        errors=[error],
    )


def _failed_read(
    request: EmailReadRequest,
    *,
    provider: str,
    output_ref: str,
    code: str,
    message: str,
    latency_ms: int = 0,
) -> EmailReadResult:
    error = _error(code, message)
    return EmailReadResult(
        success=False,
        message_ids=request.message_ids,
        summary=error.message,
        provider=provider,
        latency_ms=latency_ms,
        output_ref=output_ref,
        errors=[error],
    )


def _error(code: str, message: object) -> EmailProviderError:
    normalized_code = normalize_provider_error_code(code)
    return EmailProviderError(
        code=normalized_code,
        message=sanitize_error_message(message),
        recoverable=is_recoverable_provider_error(normalized_code),
    )


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))
