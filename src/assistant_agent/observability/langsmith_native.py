"""Native LangSmith tracing around the real LangGraph execution boundaries."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
from typing import Any

from assistant_agent.observability.langsmith_config import (
    LangSmithConfig,
    create_langsmith_client,
)
from assistant_agent.providers.provider_errors import ProviderSafetyPolicy

try:  # LangSmith remains an optional, fail-open observability dependency.
    from langsmith import traceable
    from langsmith.run_helpers import get_current_run_tree, tracing_context
except (ImportError, ModuleNotFoundError):  # pragma: no cover - optional dependency
    traceable = None  # type: ignore[assignment]
    get_current_run_tree = None  # type: ignore[assignment]
    tracing_context = None  # type: ignore[assignment]


_DROP = object()
_MAX_DEPTH = 8
_MAX_ITEMS = 64
_MAX_TEXT_CHARS = 8_000
_FORBIDDEN_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "secret",
    "password",
    "callback",
    "reasoning",
    "protocol_response",
    "raw_payload",
    "raw_provider",
    "raw_response",
    "raw_data",
    "bytes",
    "base64",
    "image",
    "audio",
    "video",
    "media",
    "path",
)
_TOOL_MESSAGE_TOP_LEVEL_FIELDS = ("tool_call_id", "name")
_TOOL_MESSAGE_TEXT_FIELDS = frozenset(
    {
        "answer",
        "status",
        "code",
        "message",
        "summary",
        "name",
        "value",
        "title",
        "kind",
        "type",
    }
)
_TOOL_MESSAGE_NUMBER_FIELDS = frozenset(
    {"count", "total", "returned_count", "index", "score", "confidence", "latency_ms"}
)
_TOOL_MESSAGE_BOOL_FIELDS = frozenset({"success", "redacted", "truncated"})
_TOOL_MESSAGE_CONTAINER_FIELDS = frozenset(
    {"nested", "data", "result", "metadata", "items"}
)
_NATIVE_TRACE_ACTIVE: ContextVar[bool] = ContextVar(
    "assistant_agent_native_langsmith_active",
    default=False,
)
_TEXT_SAFETY_POLICY = ProviderSafetyPolicy(
    max_message_chars=_MAX_TEXT_CHARS,
    max_detail_chars=_MAX_TEXT_CHARS,
)


@contextmanager
def native_langsmith_tracing(
    config: LangSmithConfig,
    *,
    metadata: dict[str, Any],
    tags: Sequence[str] = (),
) -> Iterator[None]:
    """Preserve a current LangSmith parent instead of creating a competing root."""

    if get_current_run_tree is not None:
        try:
            current_parent = get_current_run_tree()
        except Exception:
            yield
            return
    else:
        current_parent = None
    if current_parent is not None:
        token = _NATIVE_TRACE_ACTIVE.set(True)
        try:
            yield
        finally:
            _NATIVE_TRACE_ACTIVE.reset(token)
        return
    if not config.enabled or tracing_context is None:
        yield
        return
    client: Any | None = None
    try:
        client = create_langsmith_client(config)
        context = tracing_context(
            project_name=config.project,
            metadata=dict(metadata),
            tags=list(tags),
            enabled=True,
            client=client,
        )
        context.__enter__()
    except Exception:
        if client is not None:
            _close_owned_client(client)
        yield
        return
    token = _NATIVE_TRACE_ACTIVE.set(True)
    try:
        try:
            yield
        except BaseException:
            try:
                context.__exit__(*sys.exc_info())
            except Exception:
                pass
            raise
        else:
            try:
                context.__exit__(None, None, None)
            except Exception:
                pass
    finally:
        _NATIVE_TRACE_ACTIVE.reset(token)
        _close_owned_client(client)


def _close_owned_client(client: Any) -> None:
    for method_name in ("flush", "close"):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue
        try:
            method(timeout=2.0)
        except Exception:
            pass


def project_llm_inputs(
    request: Any,
    *,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Project a provider-neutral ChatRequest without identity or runtime objects."""

    return {
        "provider": _bounded_text(provider),
        "model": _bounded_text(model),
        "messages": _project_messages(getattr(request, "messages", [])),
        "tools": _safe_value(getattr(request, "tools", [])),
        "tool_choice": _safe_value(getattr(request, "tool_choice", None)),
        "response_format": _safe_value(getattr(request, "response_format", None)),
        "temperature": getattr(request, "temperature", None),
        "max_tokens": getattr(request, "max_tokens", None),
    }


def project_llm_outputs(result: Any) -> dict[str, Any]:
    """Project normalized ChatResult fields; exclude SDK envelopes and reasoning."""

    tool_calls = []
    for tool_call in list(getattr(result, "tool_calls", []) or [])[:_MAX_ITEMS]:
        dumped = (
            tool_call.model_dump(mode="json")
            if hasattr(tool_call, "model_dump")
            else tool_call
        )
        safe = _safe_value(dumped)
        if safe is not _DROP:
            tool_calls.append(safe)
    errors = []
    for error in list(getattr(result, "errors", []) or [])[:_MAX_ITEMS]:
        errors.append(
            {
                "code": _bounded_text(str(getattr(error, "code", "provider_error"))),
                "recoverable": bool(getattr(error, "recoverable", False)),
            }
        )
    usage = _safe_value(getattr(result, "usage", {}))
    return {
        "provider": _bounded_text(str(getattr(result, "provider", "unknown"))),
        "model": _optional_text(getattr(result, "model", None)),
        "response_text": _bounded_text(str(getattr(result, "response_text", ""))),
        "tool_calls": tool_calls,
        "finish_reason": _optional_text(getattr(result, "finish_reason", None)),
        "refusal": _optional_text(getattr(result, "refusal", None)),
        "usage": usage if isinstance(usage, dict) else {},
        "errors": errors,
    }


def project_tool_input(safe_input: dict[str, Any]) -> dict[str, Any]:
    """Apply a final bounded allow-safe projection to a governed Tool summary."""

    projected = _safe_value(safe_input)
    return projected if isinstance(projected, dict) else {}


def project_tool_output(result: Any) -> dict[str, Any]:
    """Project normalized ToolResult without raw payload, media, or audit internals."""

    data = getattr(result, "data", None)
    output_ref = getattr(result, "output_ref", None)
    return {
        "tool_name": _bounded_text(str(getattr(result, "tool_name", "unknown"))),
        "success": bool(getattr(result, "success", False)),
        "data_field_count": len(data) if isinstance(data, dict) else 0,
        "output_ref_present": bool(
            isinstance(output_ref, str) and output_ref.strip()
        ),
        "error_code": _tool_error_code(getattr(result, "error", None)),
    }


def native_tracing_active() -> bool:
    """Return whether this execution inherits or owns a native LangSmith context."""

    return _NATIVE_TRACE_ACTIVE.get()


def trace_llm_call(
    call: Any,
    *,
    request: Any,
    provider: str,
    model: str,
) -> Any:
    """Trace exactly one real Provider call as an ``llm`` child run."""

    inputs = project_llm_inputs(request, provider=provider, model=model)
    return _trace_call(
        call,
        name="llm.chat",
        run_type="llm",
        inputs=inputs,
        output_projector=project_llm_outputs,
    )


def trace_governed_tool_call(
    call: Any,
    *,
    tool_name: str,
    safe_input: dict[str, Any],
) -> Any:
    """Trace one backend Tool attempt without moving lifecycle governance."""

    return _trace_call(
        call,
        name=_bounded_text(tool_name),
        run_type="tool",
        inputs={
            "tool_name": _bounded_text(tool_name),
            "input": project_tool_input(safe_input),
        },
        output_projector=project_tool_output,
    )


def _trace_call(
    call: Any,
    *,
    name: str,
    run_type: str,
    inputs: dict[str, Any],
    output_projector: Any,
) -> Any:
    if not native_tracing_active() or traceable is None:
        return call()
    called = False
    result: Any = _DROP
    business_error: BaseException | None = None

    def invoke() -> Any:
        nonlocal called, result, business_error
        called = True
        try:
            result = call()
            return result
        except BaseException as exc:
            business_error = exc
            raise

    try:
        wrapped = traceable(
            name=name,
            run_type=run_type,
            process_inputs=lambda _: inputs,
            process_outputs=output_projector,
        )(invoke)
        return wrapped()
    except Exception:
        if business_error is not None:
            raise business_error
        if called and result is not _DROP:
            return result
        return call()


def _project_messages(messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []
    projected: list[dict[str, Any]] = []
    for message in messages[:_MAX_ITEMS]:
        if not isinstance(message, dict):
            continue
        safe = (
            _project_tool_message(message)
            if message.get("role") == "tool"
            else _safe_value(message)
        )
        if isinstance(safe, dict):
            projected.append(safe)
    return projected


def _project_tool_message(message: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {"role": "tool"}
    for field_name in _TOOL_MESSAGE_TOP_LEVEL_FIELDS:
        value = message.get(field_name)
        safe_label = _safe_machine_label(value)
        if safe_label is not None:
            projected[field_name] = safe_label
    content = message.get("content")
    if isinstance(content, str):
        if len(content) > _MAX_TEXT_CHARS:
            projected["content"] = _tool_content_summary(content)
            return projected
        try:
            content = json.loads(content)
        except (TypeError, ValueError, RecursionError):
            projected["content"] = _tool_content_summary(content)
            return projected
    safe_content = _safe_tool_message_value(content)
    projected["content"] = (
        safe_content
        if safe_content is not _DROP
        else _tool_content_summary(message.get("content"))
    )
    return projected


def _safe_tool_message_value(
    value: Any,
    *,
    depth: int = 0,
    key: str | None = None,
) -> Any:
    if depth > _MAX_DEPTH:
        return _DROP
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:_MAX_ITEMS]:
            item_key = str(raw_key)
            safe = _safe_tool_message_field(item_key, item, depth=depth + 1)
            if safe is not _DROP:
                projected[item_key] = safe
        return projected
    if isinstance(value, (list, tuple)) and key in _TOOL_MESSAGE_CONTAINER_FIELDS:
        projected_items = []
        for item in list(value)[:_MAX_ITEMS]:
            safe = _safe_tool_message_value(item, depth=depth + 1, key="items")
            if safe is not _DROP:
                projected_items.append(safe)
        return projected_items
    return _DROP


def _safe_tool_message_field(key: str, value: Any, *, depth: int) -> Any:
    if key in _TOOL_MESSAGE_TEXT_FIELDS and isinstance(value, str):
        return _safe_tool_message_text(value)
    if (
        key in _TOOL_MESSAGE_NUMBER_FIELDS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return value
    if key in _TOOL_MESSAGE_BOOL_FIELDS and isinstance(value, bool):
        return value
    if key in _TOOL_MESSAGE_CONTAINER_FIELDS and isinstance(
        value, (dict, list, tuple)
    ):
        return _safe_tool_message_value(value, depth=depth, key=key)
    return _DROP


def _safe_tool_message_text(value: str) -> Any:
    reserved = (":", "/", "\\", "?", "=", "&", "%")
    if not value or any(character in value for character in reserved):
        return _DROP
    return _bounded_text(value)


def _safe_machine_label(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    if not all(character.isalnum() or character in "._-" for character in value):
        return None
    return value


def _tool_content_summary(value: Any) -> dict[str, Any]:
    return {
        "redacted": True,
        "content_chars": len(value) if isinstance(value, str) else 0,
    }


def _safe_value(value: Any, *, depth: int = 0, key: str | None = None) -> Any:
    if depth > _MAX_DEPTH or _forbidden_key(key):
        return _DROP
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _DROP
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.startswith("data:") or "base64," in lowered:
            return _DROP
        return _bounded_text(value)
    if isinstance(value, dict):
        item_type = value.get("type")
        if isinstance(item_type, str) and any(
            marker in item_type.lower()
            for marker in ("image", "audio", "video", "media", "file")
        ):
            return _DROP
        projected: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:_MAX_ITEMS]:
            item_key = str(raw_key)
            safe = _safe_value(item, depth=depth + 1, key=item_key)
            if safe is not _DROP:
                projected[item_key] = safe
        return projected
    if isinstance(value, (list, tuple)):
        projected_items = []
        for item in list(value)[:_MAX_ITEMS]:
            safe = _safe_value(item, depth=depth + 1)
            if safe is not _DROP:
                projected_items.append(safe)
        return projected_items
    return _DROP


def _forbidden_key(key: str | None) -> bool:
    if key is None:
        return False
    normalized = key.strip().lower().replace("-", "_")
    return any(part in normalized for part in _FORBIDDEN_KEY_PARTS)


def _bounded_text(value: str) -> str:
    if not value:
        return ""
    return _TEXT_SAFETY_POLICY.sanitize_message(value)


def _optional_text(value: Any) -> str | None:
    return _bounded_text(value) if isinstance(value, str) else None


def _tool_error_code(error: Any) -> str | None:
    if not isinstance(error, str) or not error:
        return None
    from assistant_agent.runtime.recovery import classify_error

    return classify_error(error)
