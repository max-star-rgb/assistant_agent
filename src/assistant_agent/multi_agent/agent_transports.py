"""Transports for optional agent-to-agent communication."""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Mapping
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from assistant_agent.api.models import AgentRunResponse
from assistant_agent.multi_agent.a2a_protocol import A2A_JSONRPC_VERSION
from assistant_agent.multi_agent.models import (
    AgentArtifact,
    AgentCommunicationError,
    AgentInstance,
    AgentTask,
    AgentTaskResult,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.providers.provider_errors import sanitize_error_detail, sanitize_error_message


class AgentTransport(Protocol):
    """Protocol-neutral transport for sending a task to an agent instance."""

    name: str

    def send_task(self, task: AgentTask, *, instance: AgentInstance | None = None) -> AgentTaskResult:
        """Execute or deliver one agent task."""


class LocalAgentTransport:
    """In-process transport backed by explicitly injected agent invokers."""

    name = "local"

    def __init__(self, runtimes: dict[str, Any]) -> None:
        self._runtimes = dict(runtimes)

    def send_task(self, task: AgentTask, *, instance: AgentInstance | None = None) -> AgentTaskResult:
        runtime = self._runtimes.get(task.target_agent_id)
        if runtime is None:
            return _failed_result(
                task,
                "agent_runtime_not_found",
                f"No local runtime registered for agent: {task.target_agent_id}",
                detail={"agent_id": task.target_agent_id, "transport": self.name},
                recoverable=True,
                transport_name=self.name,
            )
        try:
            response = runtime.invoke(_request_from_task(task))
        except Exception as exc:  # pragma: no cover - defensive transport boundary
            return _failed_result(
                task,
                "agent_transport_failed",
                exc,
                detail={"agent_id": task.target_agent_id, "transport": self.name},
                transport_name=self.name,
            )
        if not isinstance(response, AgentRunResponse):
            return _failed_result(
                task,
                "agent_transport_invalid_response",
                "Local agent invoker did not return AgentRunResponse.",
                detail={"agent_id": task.target_agent_id, "transport": self.name},
                transport_name=self.name,
            )
        return _result_from_response(task, response, transport_name=self.name)


class RemoteAgentAllowlist:
    """Allowlist for explicitly configured outbound A2A endpoints."""

    def __init__(
        self,
        allowed_hosts: list[str] | tuple[str, ...] | set[str] | None = None,
        *,
        allow_http_localhost: bool = False,
    ) -> None:
        self.allowed_hosts = {
            normalized
            for value in allowed_hosts or []
            if (normalized := _normalize_host(value))
        }
        self.allow_http_localhost = allow_http_localhost

    def validate(self, endpoint_url: str) -> AgentCommunicationError | None:
        parsed = urlparse(endpoint_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return AgentCommunicationError(
                code="agent_remote_endpoint_invalid",
                message="Remote A2A endpoint URL is invalid.",
                detail={"endpoint_url": sanitize_error_message(endpoint_url)},
                recoverable=True,
            )
        host_port = _host_port(parsed)
        if parsed.scheme != "https" and not (
            self.allow_http_localhost and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            return AgentCommunicationError(
                code="agent_remote_https_required",
                message="Remote A2A endpoint must use HTTPS unless localhost HTTP is explicitly allowed.",
                detail={"scheme": parsed.scheme, "host": host_port},
                recoverable=True,
            )
        if not self.allowed_hosts:
            return AgentCommunicationError(
                code="agent_remote_allowlist_missing",
                message="Outbound A2A transport requires an explicit remote host allowlist.",
                detail={"host": host_port},
                recoverable=True,
            )
        if host_port not in self.allowed_hosts and _normalize_host(parsed.hostname) not in self.allowed_hosts:
            return AgentCommunicationError(
                code="agent_remote_host_not_allowed",
                message="Remote A2A endpoint host is not allowlisted.",
                detail={"host": host_port, "allowed_hosts": sorted(self.allowed_hosts)},
                recoverable=True,
            )
        return None


class AuthHeaderProvider:
    """Explicit outbound auth header provider for remote A2A pilot calls."""

    def __init__(self, headers_by_agent_id: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self._headers_by_agent_id = {
            agent_id: {str(key): str(value) for key, value in headers.items()}
            for agent_id, headers in (headers_by_agent_id or {}).items()
        }

    def headers_for(self, *, agent_id: str, endpoint_url: str) -> dict[str, str]:
        return dict(self._headers_by_agent_id.get(agent_id, {}))


class A2ACircuitBreaker:
    """Small per-host circuit breaker for outbound pilot calls."""

    def __init__(self, *, failure_threshold: int = 3, reset_after_seconds: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.reset_after_seconds = reset_after_seconds
        self._failures: dict[str, tuple[int, float]] = {}

    def validate(self, endpoint_url: str) -> AgentCommunicationError | None:
        host = _host_port(urlparse(endpoint_url))
        count, last_failed_at = self._failures.get(host, (0, 0.0))
        if count < self.failure_threshold:
            return None
        if time.monotonic() - last_failed_at >= self.reset_after_seconds:
            self._failures.pop(host, None)
            return None
        return AgentCommunicationError(
            code="agent_remote_circuit_open",
            message="Remote A2A endpoint circuit breaker is open.",
            detail={"host": host, "failure_threshold": self.failure_threshold},
            recoverable=True,
        )

    def record_success(self, endpoint_url: str) -> None:
        self._failures.pop(_host_port(urlparse(endpoint_url)), None)

    def record_failure(self, endpoint_url: str) -> None:
        host = _host_port(urlparse(endpoint_url))
        count, _ = self._failures.get(host, (0, 0.0))
        self._failures[host] = (count + 1, time.monotonic())


class AgentCardFetcher:
    """Fetch a bounded public A2A Agent Card for an already allowlisted endpoint."""

    def __init__(self, *, max_card_bytes: int = 64_000) -> None:
        self.max_card_bytes = max_card_bytes

    def fetch(
        self,
        endpoint_url: str,
        *,
        timeout_ms: int,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any] | None, AgentCommunicationError | None]:
        card_url = _agent_card_url(endpoint_url)
        request = Request(
            card_url,
            headers={"accept": "application/json", **dict(headers or {})},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_ms / 1000) as response:
                body = response.read(self.max_card_bytes + 1)
        except TimeoutError:
            return None, _agent_card_error(
                "agent_remote_card_timeout",
                "Remote A2A Agent Card request timed out.",
            )
        except socket.timeout:
            return None, _agent_card_error(
                "agent_remote_card_timeout",
                "Remote A2A Agent Card request timed out.",
            )
        except HTTPError as exc:
            return None, _agent_card_error(
                "agent_remote_card_unavailable",
                f"Remote A2A Agent Card returned HTTP {exc.code}.",
                detail={"status_code": exc.code},
            )
        except (OSError, URLError) as exc:
            return None, _agent_card_error(
                "agent_remote_card_unavailable",
                "Remote A2A Agent Card request failed.",
                detail={"reason": sanitize_error_message(exc)},
            )
        if len(body) > self.max_card_bytes:
            return None, _agent_card_error(
                "agent_remote_card_too_large",
                "Remote A2A Agent Card exceeds transport limit.",
                detail={"max_card_bytes": self.max_card_bytes},
                recoverable=False,
            )
        try:
            card = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, _agent_card_error(
                "agent_remote_card_invalid",
                "Remote A2A Agent Card is not valid JSON.",
                detail={"error": str(exc)},
            )
        if not isinstance(card, dict):
            return None, _agent_card_error(
                "agent_remote_card_invalid",
                "Remote A2A Agent Card must be a JSON object.",
            )
        return card, None


class AgentCardValidator:
    """Validate public card metadata without auto-enabling remote agents."""

    def validate(
        self,
        card: Mapping[str, Any],
        *,
        endpoint_url: str,
    ) -> AgentCommunicationError | None:
        if not card.get("name"):
            return _agent_card_error("agent_remote_card_invalid", "Remote A2A Agent Card is missing name.")
        card_endpoint = str(card.get("url") or "").strip()
        if card_endpoint and _host_port(urlparse(card_endpoint)) != _host_port(urlparse(endpoint_url)):
            return _agent_card_error(
                "agent_remote_card_endpoint_mismatch",
                "Remote A2A Agent Card URL host does not match the configured endpoint.",
                detail={
                    "card_host": _host_port(urlparse(card_endpoint)),
                    "endpoint_host": _host_port(urlparse(endpoint_url)),
                },
                recoverable=False,
            )
        methods = card.get("supportedMethods")
        if isinstance(methods, list) and not {"message/send", "SendMessage"}.intersection(set(methods)):
            return _agent_card_error(
                "agent_remote_card_method_unsupported",
                "Remote A2A Agent Card does not advertise message/send.",
                recoverable=True,
            )
        return None


class A2AJsonRpcTransport:
    """Outbound A2A JSON-RPC transport for explicitly allowlisted pilot agents."""

    name = "a2a_json_rpc"

    def __init__(
        self,
        *,
        allowlist: RemoteAgentAllowlist | None = None,
        endpoints: Mapping[str, str] | None = None,
        auth_headers: AuthHeaderProvider | None = None,
        circuit_breaker: A2ACircuitBreaker | None = None,
        agent_card_fetcher: AgentCardFetcher | None = None,
        agent_card_validator: AgentCardValidator | None = None,
        require_agent_card: bool = False,
        max_payload_bytes: int = 64_000,
        max_response_bytes: int = 256_000,
    ) -> None:
        self.allowlist = allowlist or RemoteAgentAllowlist()
        self.endpoints = {str(agent_id): str(endpoint) for agent_id, endpoint in (endpoints or {}).items()}
        self.auth_headers = auth_headers or AuthHeaderProvider()
        self.circuit_breaker = circuit_breaker or A2ACircuitBreaker()
        self.agent_card_fetcher = agent_card_fetcher or AgentCardFetcher()
        self.agent_card_validator = agent_card_validator or AgentCardValidator()
        self.require_agent_card = require_agent_card
        self.max_payload_bytes = max_payload_bytes
        self.max_response_bytes = max_response_bytes

    def send_task(self, task: AgentTask, *, instance: AgentInstance | None = None) -> AgentTaskResult:
        endpoint_url = _endpoint_for_task(task, instance=instance, endpoints=self.endpoints)
        if not endpoint_url:
            return _failed_result(
                task,
                "agent_remote_endpoint_missing",
                "Remote A2A endpoint is not configured for the target agent.",
                detail={"agent_id": task.target_agent_id, "transport": self.name},
                recoverable=True,
                transport_name=self.name,
            )
        policy_error = self.allowlist.validate(endpoint_url) or self.circuit_breaker.validate(endpoint_url)
        if policy_error is not None:
            return _failed_result(
                task,
                policy_error.code,
                policy_error.message,
                detail=policy_error.detail,
                recoverable=policy_error.recoverable,
                transport_name=self.name,
            )
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            **self.auth_headers.headers_for(agent_id=task.target_agent_id, endpoint_url=endpoint_url),
        }
        if self.require_agent_card:
            card, card_error = self.agent_card_fetcher.fetch(
                endpoint_url,
                timeout_ms=task.timeout_ms,
                headers=headers,
            )
            if card_error is not None:
                return _failed_result(
                    task,
                    card_error.code,
                    card_error.message,
                    detail=card_error.detail,
                    recoverable=card_error.recoverable,
                    transport_name=self.name,
                )
            validation_error = self.agent_card_validator.validate(card or {}, endpoint_url=endpoint_url)
            if validation_error is not None:
                return _failed_result(
                    task,
                    validation_error.code,
                    validation_error.message,
                    detail=validation_error.detail,
                    recoverable=validation_error.recoverable,
                    transport_name=self.name,
                )
        payload = _a2a_request_payload(task)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self.max_payload_bytes:
            return _failed_result(
                task,
                "agent_remote_payload_too_large",
                "Remote A2A request payload exceeds transport limit.",
                detail={"payload_bytes": len(body), "max_payload_bytes": self.max_payload_bytes},
                recoverable=False,
                transport_name=self.name,
            )
        request = Request(endpoint_url, data=body, headers=headers, method="POST")
        try:
            # The URL has already passed the explicit allowlist check above.
            with urlopen(request, timeout=task.timeout_ms / 1000) as response:
                response_body = response.read(self.max_response_bytes + 1)
        except TimeoutError:
            self.circuit_breaker.record_failure(endpoint_url)
            return _remote_timeout_result(task)
        except socket.timeout:
            self.circuit_breaker.record_failure(endpoint_url)
            return _remote_timeout_result(task)
        except HTTPError as exc:
            self.circuit_breaker.record_failure(endpoint_url)
            return _failed_result(
                task,
                "agent_remote_http_error",
                f"Remote A2A endpoint returned HTTP {exc.code}.",
                detail={"status_code": exc.code, "endpoint_host": _host_port(urlparse(endpoint_url))},
                recoverable=exc.code in {408, 429} or exc.code >= 500,
                transport_name=self.name,
            )
        except URLError as exc:
            self.circuit_breaker.record_failure(endpoint_url)
            if _is_timeout_reason(exc.reason):
                return _remote_timeout_result(task)
            return _failed_result(
                task,
                "agent_remote_network_error",
                "Remote A2A endpoint network request failed.",
                detail={"reason": sanitize_error_message(exc.reason)},
                recoverable=True,
                transport_name=self.name,
            )
        except OSError as exc:
            self.circuit_breaker.record_failure(endpoint_url)
            return _failed_result(
                task,
                "agent_remote_network_error",
                exc,
                detail={"endpoint_host": _host_port(urlparse(endpoint_url))},
                recoverable=True,
                transport_name=self.name,
            )
        if len(response_body) > self.max_response_bytes:
            self.circuit_breaker.record_failure(endpoint_url)
            return _failed_result(
                task,
                "agent_remote_response_too_large",
                "Remote A2A response payload exceeds transport limit.",
                detail={"max_response_bytes": self.max_response_bytes},
                recoverable=False,
                transport_name=self.name,
            )
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.circuit_breaker.record_failure(endpoint_url)
            return _protocol_error_result(
                task,
                "Remote A2A response is not valid JSON.",
                detail={"error": str(exc)},
            )
        result = _result_from_a2a_payload(task, payload)
        if result.status == "failed":
            self.circuit_breaker.record_failure(endpoint_url)
        else:
            self.circuit_breaker.record_success(endpoint_url)
        metadata = {
            **result.metadata,
            "transport": self.name,
            "endpoint_host": _host_port(urlparse(endpoint_url)),
        }
        return result.model_copy(update={"metadata": metadata}, deep=True)


def _request_from_task(task: AgentTask) -> UserRequest:
    metadata = {
        **task.message.metadata,
        **task.metadata,
        "agent_communication": {
            "task_id": task.task_id,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "parent_run_id": task.session.parent_run_id,
            "parent_trace_id": task.session.parent_trace_id,
            "correlation_id": task.session.correlation_id,
            "delegation_depth": task.delegation_depth,
            "max_delegation_depth": task.max_delegation_depth,
            "delegation_pairs": task.metadata.get("delegation_pairs", []),
            "timeout_ms": task.timeout_ms,
            "token_budget": task.token_budget,
            "tool_budget": task.tool_budget,
            "transport": "local",
        },
    }
    return UserRequest(
        user_id=task.session.user_id,
        session_id=task.session.session_id,
        text=task.message.text,
        image_ids=list(task.message.image_ids),
        video_ids=list(task.message.video_ids),
        audio_id=task.message.audio_id,
        metadata=sanitize_error_detail(metadata),
    )


def _result_from_response(
    task: AgentTask,
    response: AgentRunResponse,
    *,
    transport_name: str,
) -> AgentTaskResult:
    artifacts = []
    if response.response_text:
        artifacts.append(
            AgentArtifact(
                kind="text",
                text=response.response_text,
                data=sanitize_error_detail(response.data),
                metadata={"source": "agent_response"},
            )
        )
    errors = [
        AgentCommunicationError(
            code=error.code or "agent_run_error",
            message=sanitize_error_message(error.message),
            detail=sanitize_error_detail(error.detail),
            recoverable=error.recoverable,
        )
        for error in response.errors
    ]
    return AgentTaskResult(
        task_id=task.task_id,
        target_agent_id=task.target_agent_id,
        status=_agent_task_result_status(response.status),
        artifacts=artifacts,
        run_id=response.run_id,
        trace_id=response.trace_id,
        errors=errors,
        metadata={
            "transport": transport_name,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "correlation_id": task.session.correlation_id,
        },
    )


def _agent_task_result_status(status: str) -> Literal["completed", "failed", "cancelled"]:
    if status == "failed":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "completed"


def _failed_result(
    task: AgentTask,
    code: str,
    message: object,
    *,
    detail: dict[str, Any] | None = None,
    recoverable: bool = False,
    transport_name: str = "local",
) -> AgentTaskResult:
    return AgentTaskResult(
        task_id=task.task_id,
        target_agent_id=task.target_agent_id,
        status="failed",
        errors=[
            AgentCommunicationError(
                code=code,
                message=sanitize_error_message(message),
                detail=sanitize_error_detail(detail or {}),
                recoverable=recoverable,
            )
        ],
        metadata={
            "transport": transport_name,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "correlation_id": task.session.correlation_id,
        },
    )


def _endpoint_for_task(
    task: AgentTask,
    *,
    instance: AgentInstance | None,
    endpoints: Mapping[str, str],
) -> str | None:
    if instance is not None and instance.endpoint_url:
        return instance.endpoint_url
    return endpoints.get(task.target_agent_id)


def _a2a_request_payload(task: AgentTask) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if task.message.text:
        parts.append({"kind": "text", "text": task.message.text})
    parts.extend(
        {"kind": "file", "file": {"uri": image_id, "mimeType": "image/*"}}
        for image_id in task.message.image_ids
    )
    parts.extend(
        {"kind": "file", "file": {"uri": video_id, "mimeType": "video/*"}}
        for video_id in task.message.video_ids
    )
    if task.message.audio_id:
        parts.append({"kind": "file", "file": {"uri": task.message.audio_id, "mimeType": "audio/*"}})
    metadata = sanitize_error_detail(
        {
            **task.message.metadata,
            "user_id": task.session.user_id,
            "session_id": task.session.session_id,
            "source_agent_id": task.source_agent_id,
            "target_agent_id": task.target_agent_id,
            "parent_run_id": task.session.parent_run_id,
            "parent_trace_id": task.session.parent_trace_id,
            "correlation_id": task.session.correlation_id,
            "delegation_depth": task.delegation_depth,
            "max_delegation_depth": task.max_delegation_depth,
            "token_budget": task.token_budget,
            "tool_budget": task.tool_budget,
        }
    )
    return {
        "jsonrpc": A2A_JSONRPC_VERSION,
        "id": task.task_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": task.message.role,
                "messageId": task.task_id,
                "contextId": task.session.session_id,
                "parts": parts,
                "metadata": metadata,
            },
            "metadata": sanitize_error_detail(task.metadata),
        },
    }


def _result_from_a2a_payload(task: AgentTask, payload: Any) -> AgentTaskResult:
    if not isinstance(payload, dict):
        return _protocol_error_result(task, "Remote A2A response must be a JSON object.")
    if payload.get("jsonrpc") != A2A_JSONRPC_VERSION:
        return _protocol_error_result(task, "Remote A2A response has invalid JSON-RPC version.")
    if payload.get("id") not in {task.task_id, None}:
        return _protocol_error_result(
            task,
            "Remote A2A response id does not match the request id.",
            detail={"request_id": task.task_id, "response_id": payload.get("id")},
        )
    error = payload.get("error")
    if error is not None:
        detail = error if isinstance(error, dict) else {"error": sanitize_error_message(error)}
        return _protocol_error_result(task, "Remote A2A JSON-RPC error.", detail=detail)
    result = payload.get("result")
    if not isinstance(result, dict):
        return _protocol_error_result(task, "Remote A2A response result must be an object.")

    status_obj = result.get("status") if isinstance(result.get("status"), dict) else {}
    state = sanitize_error_message(status_obj.get("state") or "completed")
    artifacts = _artifacts_from_a2a_result(result, status_obj=status_obj)
    metadata = {
        "transport": "a2a_json_rpc",
        "source_agent_id": task.source_agent_id,
        "target_agent_id": task.target_agent_id,
        "correlation_id": task.session.correlation_id,
        "remote_task_id": sanitize_error_message(result.get("id") or ""),
        "remote_context_id": sanitize_error_message(result.get("contextId") or ""),
        "remote_status_state": state,
        "remote_metadata": sanitize_error_detail(
            result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        ),
    }
    if state == "failed":
        message = _status_message_text(status_obj) or "Remote A2A task failed."
        return AgentTaskResult(
            task_id=task.task_id,
            target_agent_id=task.target_agent_id,
            status="failed",
            artifacts=artifacts,
            run_id=metadata["remote_task_id"] or None,
            trace_id=_remote_trace_id(metadata["remote_metadata"]),
            errors=[
                AgentCommunicationError(
                    code="agent_remote_business_failed",
                    message=sanitize_error_message(message),
                    detail=sanitize_error_detail(
                        {
                            "remote_task_id": metadata["remote_task_id"],
                            "remote_status_state": state,
                        }
                    ),
                    recoverable=True,
                )
            ],
            metadata=metadata,
        )
    return AgentTaskResult(
        task_id=task.task_id,
        target_agent_id=task.target_agent_id,
        status="completed",
        artifacts=artifacts,
        run_id=metadata["remote_task_id"] or None,
        trace_id=_remote_trace_id(metadata["remote_metadata"]),
        metadata=metadata,
    )


def _artifacts_from_a2a_result(result: dict[str, Any], *, status_obj: dict[str, Any]) -> list[AgentArtifact]:
    artifacts: list[AgentArtifact] = []
    raw_artifacts = result.get("artifacts")
    if isinstance(raw_artifacts, list):
        for raw_artifact in raw_artifacts[:20]:
            if not isinstance(raw_artifact, dict):
                continue
            artifacts.extend(_artifacts_from_parts(raw_artifact.get("parts"), metadata_source=raw_artifact))
    if not artifacts:
        status_text = _status_message_text(status_obj)
        if status_text:
            artifacts.append(
                AgentArtifact(
                    kind="text",
                    text=status_text,
                    metadata={"source": "a2a_status_message"},
                )
            )
    return artifacts


def _artifacts_from_parts(parts: Any, *, metadata_source: dict[str, Any]) -> list[AgentArtifact]:
    if not isinstance(parts, list):
        return []
    artifacts: list[AgentArtifact] = []
    for part in parts[:20]:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            artifacts.append(
                AgentArtifact(
                    kind="text",
                    text=sanitize_error_message(text),
                    metadata={
                        "source": "a2a_artifact",
                        "remote_artifact_id": sanitize_error_message(metadata_source.get("artifactId") or ""),
                        "remote_artifact_name": sanitize_error_message(metadata_source.get("name") or ""),
                    },
                )
            )
            continue
        file_payload = part.get("file") if isinstance(part.get("file"), dict) else {}
        output_ref = part.get("uri") or part.get("url") or file_payload.get("uri") or file_payload.get("url")
        if isinstance(output_ref, str) and output_ref:
            artifacts.append(
                AgentArtifact(
                    kind="output_ref",
                    output_refs=[sanitize_error_message(output_ref)],
                    metadata={"source": "a2a_artifact"},
                )
            )
    return artifacts


def _status_message_text(status_obj: dict[str, Any]) -> str:
    message = status_obj.get("message")
    if not isinstance(message, dict):
        return ""
    texts = []
    parts = message.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]:
                texts.append(part["text"])
    return sanitize_error_message("\n".join(texts).strip())


def _remote_trace_id(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("trace_id") or metadata.get("traceId")
    return sanitize_error_message(value) if value else None


def _protocol_error_result(
    task: AgentTask,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
) -> AgentTaskResult:
    return _failed_result(
        task,
        "agent_remote_protocol_error",
        message,
        detail=detail or {},
        recoverable=True,
        transport_name="a2a_json_rpc",
    )


def _remote_timeout_result(task: AgentTask) -> AgentTaskResult:
    return _failed_result(
        task,
        "agent_remote_timeout",
        "Remote A2A endpoint timed out.",
        detail={"timeout_ms": task.timeout_ms},
        recoverable=True,
        transport_name="a2a_json_rpc",
    )


def _agent_card_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/agent-card.json"


def _agent_card_error(
    code: str,
    message: str,
    *,
    detail: dict[str, Any] | None = None,
    recoverable: bool = True,
) -> AgentCommunicationError:
    return AgentCommunicationError(
        code=code,
        message=message,
        detail=sanitize_error_detail(detail or {}),
        recoverable=recoverable,
    )


def _normalize_host(value: str) -> str:
    text = str(value).strip().lower()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    if parsed.hostname:
        if parsed.port is not None:
            return f"{parsed.hostname.lower()}:{parsed.port}"
        return parsed.hostname.lower()
    return text


def _host_port(parsed: Any) -> str:
    hostname = (parsed.hostname or "").lower()
    if parsed.port is None:
        return hostname
    return f"{hostname}:{parsed.port}"


def _is_timeout_reason(reason: Any) -> bool:
    return isinstance(reason, TimeoutError | socket.timeout) or "timed out" in str(reason).lower()
