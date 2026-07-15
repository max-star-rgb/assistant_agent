"""Prompt-safe operational logs for Gateway and assistant runtime activity."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any

from assistant_agent.gateway.observability import GatewayLifecycleEvent


GATEWAY_LOGGER_NAME = "assistant_agent.gateway.lifecycle"
RUNTIME_LOGGER_NAME = "assistant_agent.runtime.trace"
DEFAULT_OPERATIONAL_LOG_DIR = Path(".data/logs")
DEFAULT_OPERATIONAL_LOG_LEVEL = "INFO"
OPERATIONAL_LOG_MAX_BYTES = 5 * 1024 * 1024
OPERATIONAL_LOG_BACKUP_COUNT = 3
OPERATIONAL_LOGGING_ENABLED_ENV = "MULTIMODAL_AGENT_OPERATIONAL_LOGGING_ENABLED"
OPERATIONAL_LOG_DIR_ENV = "MULTIMODAL_AGENT_OPERATIONAL_LOG_DIR"
OPERATIONAL_LOG_LEVEL_ENV = "MULTIMODAL_AGENT_OPERATIONAL_LOG_LEVEL"

_PACKAGE_LOGGER_NAME = "assistant_agent"
_HANDLER_MARKER = "_assistant_agent_operational_handler"
_CONFIG_LOCK = RLock()
_GATEWAY_PAYLOAD_FIELDS = frozenset(
    {
        "active_count",
        "active_runs",
        "cancel_phase",
        "created",
        "disposition",
        "expects_reply",
        "global_queue_depth",
        "handled_by",
        "limit",
        "max_active_runs",
        "newly_marked",
        "phase",
        "queue_depth",
        "queue_reason",
        "queue_wait_ms",
        "reason",
        "resumed",
        "scope",
        "source",
        "status",
    }
)
_GATEWAY_SAFE_REASONS = frozenset(
    {
        "cancelled",
        "completed",
        "error",
        "interrupted_by_new_turn",
        "queue_overflow",
        "queue_wait_timeout",
        "run_deadline_expired",
        "semantic_interrupt",
        "session_closed",
    }
)


class _UtcOperationalFormatter(logging.Formatter):
    converter = time.gmtime


class _ExactLoggerFilter(logging.Filter):
    def __init__(self, logger_name: str) -> None:
        super().__init__()
        self._logger_name = logger_name

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == self._logger_name


class _ContextDefaultsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        defaults = {
            "component": "application",
            "event": "log",
            "run_id": "-",
            "turn_id": "-",
            "trace_id": "-",
        }
        for name, value in defaults.items():
            if not hasattr(record, name):
                setattr(record, name, value)
        return True


def configure_operational_logging(
    log_dir: Path,
    level: str = DEFAULT_OPERATIONAL_LOG_LEVEL,
) -> None:
    """Configure one combined console and isolated rotating component files."""

    try:
        resolved_level = _resolve_level(level)
    except (TypeError, ValueError):
        resolved_level = logging.INFO
    resolved_dir = Path(log_dir)
    with _CONFIG_LOCK:
        reset_operational_logging_for_tests()
        formatter = _UtcOperationalFormatter(
            "%(asctime)s.%(msecs)03dZ level=%(levelname)s component=%(component)s "
            "event=%(event)s run_id=%(run_id)s turn_id=%(turn_id)s "
            "trace_id=%(trace_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

        package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
        package_logger.setLevel(resolved_level)
        package_logger.propagate = False
        console = logging.StreamHandler()
        _mark_handler(console, "console")
        console.addFilter(_ContextDefaultsFilter())
        console.setLevel(resolved_level)
        console.setFormatter(formatter)
        package_logger.addHandler(console)

        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            package_logger.warning("operational file logging unavailable")
            return

        for logger_name, filename in (
            (GATEWAY_LOGGER_NAME, "gateway.log"),
            (RUNTIME_LOGGER_NAME, "runtime.log"),
        ):
            component_logger = logging.getLogger(logger_name)
            component_logger.setLevel(resolved_level)
            component_logger.propagate = True
            try:
                handler = RotatingFileHandler(
                    resolved_dir / filename,
                    maxBytes=OPERATIONAL_LOG_MAX_BYTES,
                    backupCount=OPERATIONAL_LOG_BACKUP_COUNT,
                    encoding="utf-8",
                )
            except OSError:
                package_logger.warning(
                    "operational component file unavailable",
                    extra={
                        "component": logger_name.rsplit(".", 1)[-1],
                        "event": "logging.file_unavailable",
                        "run_id": "-",
                        "turn_id": "-",
                        "trace_id": "-",
                    },
                )
                continue
            _mark_handler(handler, filename)
            handler.addFilter(_ExactLoggerFilter(logger_name))
            handler.setLevel(resolved_level)
            handler.setFormatter(formatter)
            component_logger.addHandler(handler)


def configure_operational_logging_from_env(
    env: Mapping[str, str] | None = None,
) -> None:
    """Configure inside the serving process when explicitly enabled by launcher."""

    source = os.environ if env is None else env
    if str(source.get(OPERATIONAL_LOGGING_ENABLED_ENV, "")).lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    configure_operational_logging(
        Path(source.get(OPERATIONAL_LOG_DIR_ENV) or DEFAULT_OPERATIONAL_LOG_DIR),
        source.get(OPERATIONAL_LOG_LEVEL_ENV) or DEFAULT_OPERATIONAL_LOG_LEVEL,
    )


def reset_operational_logging_for_tests() -> None:
    """Remove handlers owned by this module; intended for tests and reload setup."""

    with _CONFIG_LOCK:
        for logger_name in (
            _PACKAGE_LOGGER_NAME,
            GATEWAY_LOGGER_NAME,
            RUNTIME_LOGGER_NAME,
        ):
            logger = logging.getLogger(logger_name)
            for handler in list(logger.handlers):
                if not getattr(handler, _HANDLER_MARKER, False):
                    continue
                logger.removeHandler(handler)
                handler.close()
        logging.getLogger(_PACKAGE_LOGGER_NAME).propagate = True


def digest_identifier(value: str | None) -> str:
    """Return a stable short digest suitable for operational correlation."""

    if not value:
        return "-"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def log_gateway_lifecycle(event: GatewayLifecycleEvent) -> None:
    """Project one prompt-safe Gateway lifecycle event to its component logger."""

    try:
        details = {
            key: _gateway_payload_value(key, value)
            for key, value in event.payload.items()
            if key in _GATEWAY_PAYLOAD_FIELDS and _is_scalar(value)
        }
        message_fields = {
            "user_id": digest_identifier(event.user_id),
            "session_id": digest_identifier(event.session_id),
            **details,
        }
        logging.getLogger(GATEWAY_LOGGER_NAME).info(
            _key_value_text(message_fields),
            extra=_record_context(
                component="gateway",
                event=event.type,
                run_id=event.run_id,
                turn_id=event.turn_id,
                trace_id=_safe_identifier(event.payload.get("trace_id")),
            ),
        )
    except Exception:  # noqa: BLE001 - operational logging is fail-open.
        return


class OperationalTraceLogStore:
    """Write-only prompt-safe projection of redacted trace events."""

    def append(self, event: Any) -> None:
        from assistant_agent.services.trace_store import redact_trace_event

        redacted = redact_trace_event(event)
        turn_id = _safe_identifier(redacted.attributes.get("turn_id"))
        fields = {
            "user_id": digest_identifier(redacted.user_id),
            "session_id": digest_identifier(redacted.session_id),
            "status": redacted.status,
            "tool": redacted.tool_name,
            "provider": redacted.provider,
            "model": redacted.model,
            "latency_ms": redacted.latency_ms,
            "error_code": redacted.error_code,
        }
        logging.getLogger(RUNTIME_LOGGER_NAME).info(
            _key_value_text(fields),
            extra=_record_context(
                component="runtime",
                event=redacted.canonical_event or redacted.event_type,
                run_id=redacted.run_id,
                turn_id=turn_id,
                trace_id=redacted.trace_id,
            ),
        )

    def list_by_run(self, run_id: str) -> list[Any]:
        return []

    def list_by_trace(self, trace_id: str) -> list[Any]:
        return []

    def node_path(self, run_id: str) -> list[str]:
        return []

    def list_by_user(self, user_id: str) -> list[Any]:
        return []

    def delete_by_user(self, user_id: str) -> int:
        return 0

    def close(self, *, timeout: float) -> bool:
        return timeout >= 0


def _resolve_level(level: str) -> int:
    normalized = str(level).upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError(f"unsupported operational log level: {level}")
    return int(getattr(logging, normalized))


def _record_context(
    *,
    component: str,
    event: str,
    run_id: str | None,
    turn_id: str | None,
    trace_id: str | None,
) -> dict[str, str]:
    return {
        "component": _safe_token(component),
        "event": _safe_token(event),
        "run_id": _safe_token(run_id),
        "turn_id": _safe_token(turn_id),
        "trace_id": _safe_token(trace_id),
    }


def _key_value_text(fields: Mapping[str, Any]) -> str:
    return " ".join(
        f"{key}={_safe_token(value)}"
        for key, value in fields.items()
        if value is not None
    )


def _safe_identifier(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _safe_token(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return "_".join(str(value).split())


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _gateway_payload_value(key: str, value: Any) -> Any:
    if key == "reason" and isinstance(value, str) and value not in _GATEWAY_SAFE_REASONS:
        return "client_supplied"
    return value


def _mark_handler(handler: logging.Handler, role: str) -> None:
    setattr(handler, _HANDLER_MARKER, True)
    setattr(handler, "_assistant_agent_operational_role", role)
