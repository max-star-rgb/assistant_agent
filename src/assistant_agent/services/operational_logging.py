"""Prompt-safe operational logs for Gateway lifecycle activity."""

from __future__ import annotations

import logging
import os
import sys
import time
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any

from assistant_agent.gateway.observability import (
    GatewayLifecycleEvent,
    JsonlGatewayLifecycleStore,
    digest_gateway_identifier,
    gateway_lifecycle_attributes,
)


GATEWAY_LOGGER_NAME = "assistant_agent.gateway.lifecycle"
_LEGACY_RUNTIME_LOGGER_NAME = "assistant_agent.runtime.trace"
DEFAULT_OPERATIONAL_LOG_DIR = Path(".data/logs")
DEFAULT_OPERATIONAL_LOG_LEVEL = "INFO"
DEFAULT_OPERATIONAL_CONSOLE_LEVEL = "INFO"
DEFAULT_OPERATIONAL_FILE_LEVEL = "DEBUG"
DEFAULT_OPERATIONAL_CONSOLE_MODE = "concise"
DEFAULT_GATEWAY_EVENT_PATH = Path(".data/gateway_events.jsonl")
OPERATIONAL_LOG_MAX_BYTES = 5 * 1024 * 1024
OPERATIONAL_LOG_BACKUP_COUNT = 3
OPERATIONAL_LOGGING_ENABLED_ENV = "MULTIMODAL_AGENT_OPERATIONAL_LOGGING_ENABLED"
OPERATIONAL_LOG_DIR_ENV = "MULTIMODAL_AGENT_OPERATIONAL_LOG_DIR"
OPERATIONAL_LOG_LEVEL_ENV = "MULTIMODAL_AGENT_OPERATIONAL_LOG_LEVEL"
OPERATIONAL_CONSOLE_LEVEL_ENV = "MULTIMODAL_AGENT_OPERATIONAL_CONSOLE_LEVEL"
OPERATIONAL_FILE_LEVEL_ENV = "MULTIMODAL_AGENT_OPERATIONAL_FILE_LEVEL"
OPERATIONAL_CONSOLE_MODE_ENV = "MULTIMODAL_AGENT_OPERATIONAL_CONSOLE_MODE"
GATEWAY_EVENT_PATH_ENV = "MULTIMODAL_AGENT_GATEWAY_EVENT_PATH"

_PACKAGE_LOGGER_NAME = "assistant_agent"
_HANDLER_MARKER = "_assistant_agent_operational_handler"
_CONFIG_LOCK = RLock()
_GATEWAY_EVENT_STORE: JsonlGatewayLifecycleStore | None = None
_CONCISE_GATEWAY_EVENTS = frozenset(
    {
        "gateway.run.started",
        "gateway.run.queued",
        "gateway.run.queue_rejected",
        "gateway.run.queue_expired",
        "gateway.run.cancel_requested",
        "gateway.run.cancelled",
        "gateway.run.completed",
        "gateway.run.errored",
        "gateway.session.acquired",
        "gateway.session.destroyed",
        "gateway.session.hangup_marked",
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


class _ConsoleRecordFilter(logging.Filter):
    def __init__(
        self,
        *,
        minimum_level: int,
        maximum_level: int | None,
        mode: str,
    ) -> None:
        super().__init__()
        self._minimum_level = minimum_level
        self._maximum_level = maximum_level
        self._mode = mode

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < self._minimum_level:
            return False
        if self._maximum_level is not None and record.levelno >= self._maximum_level:
            return False
        component = str(getattr(record, "component", "application"))
        if component == "runtime":
            return False
        if self._mode == "verbose" or record.levelno >= logging.WARNING:
            return True
        event = str(getattr(record, "event", "log"))
        if component == "gateway":
            return event in _CONCISE_GATEWAY_EVENTS
        return record.levelno >= logging.WARNING


class _HumanConsoleFormatter(_UtcOperationalFormatter):
    def __init__(self, *, mode: str) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._mode = mode

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        component = str(getattr(record, "component", "application"))
        event = str(getattr(record, "event", "log"))
        label = _console_label(record.levelname, event)
        if component != "gateway":
            return f"{timestamp} {label:<7} {record.name}"
        identifiers = _console_identifiers(record)
        suffix = f" {identifiers}" if identifiers else ""
        if self._mode == "verbose":
            message = record.getMessage()
            if message:
                suffix += f" {message}"
        return f"{timestamp} {label:<7} {event}{suffix}"


def configure_operational_logging(
    log_dir: Path,
    level: str | None = None,
    *,
    console_level: str = DEFAULT_OPERATIONAL_CONSOLE_LEVEL,
    file_level: str = DEFAULT_OPERATIONAL_FILE_LEVEL,
    console_mode: str = DEFAULT_OPERATIONAL_CONSOLE_MODE,
    gateway_event_path: Path | None = None,
) -> None:
    """Configure a human console and isolated rotating component files.

    ``level`` is retained as a compatibility shorthand that sets both console
    and file levels. New callers should use the explicit level arguments.
    """

    try:
        if level is not None:
            console_level = level
            file_level = level
        resolved_console_level = _resolve_level(console_level)
        resolved_file_level = _resolve_level(file_level)
    except (TypeError, ValueError):
        resolved_console_level = logging.INFO
        resolved_file_level = logging.INFO
    resolved_console_mode = _resolve_console_mode(console_mode)
    resolved_dir = Path(log_dir)
    with _CONFIG_LOCK:
        reset_operational_logging_for_tests()
        file_formatter = _UtcOperationalFormatter(
            "%(asctime)s.%(msecs)03dZ level=%(levelname)s component=%(component)s "
            "event=%(event)s run_id=%(run_id)s turn_id=%(turn_id)s "
            "trace_id=%(trace_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

        package_logger = logging.getLogger(_PACKAGE_LOGGER_NAME)
        package_logger.setLevel(resolved_console_level)
        package_logger.propagate = True
        for role, stream, minimum_level, maximum_level in (
            ("console.stdout", sys.stdout, resolved_console_level, logging.WARNING),
            (
                "console.stderr",
                sys.stderr,
                max(resolved_console_level, logging.WARNING),
                None,
            ),
        ):
            console = logging.StreamHandler(stream)
            _mark_handler(console, role)
            console.addFilter(_ContextDefaultsFilter())
            console.addFilter(
                _ConsoleRecordFilter(
                    minimum_level=minimum_level,
                    maximum_level=maximum_level,
                    mode=resolved_console_mode,
                )
            )
            console.setLevel(logging.DEBUG)
            console.setFormatter(_HumanConsoleFormatter(mode=resolved_console_mode))
            package_logger.addHandler(console)

        _configure_gateway_event_store(gateway_event_path, package_logger=package_logger)

        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            package_logger.warning("operational file logging unavailable")
            return

        for logger_name, filename in ((GATEWAY_LOGGER_NAME, "gateway.log"),):
            component_logger = logging.getLogger(logger_name)
            component_logger.setLevel(min(resolved_console_level, resolved_file_level))
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
            handler.setLevel(resolved_file_level)
            handler.setFormatter(file_formatter)
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
    legacy_level = source.get(OPERATIONAL_LOG_LEVEL_ENV)
    gateway_event_path_value = source.get(GATEWAY_EVENT_PATH_ENV)
    configure_operational_logging(
        Path(source.get(OPERATIONAL_LOG_DIR_ENV) or DEFAULT_OPERATIONAL_LOG_DIR),
        legacy_level,
        console_level=(
            source.get(OPERATIONAL_CONSOLE_LEVEL_ENV)
            or DEFAULT_OPERATIONAL_CONSOLE_LEVEL
        ),
        file_level=(
            source.get(OPERATIONAL_FILE_LEVEL_ENV) or DEFAULT_OPERATIONAL_FILE_LEVEL
        ),
        console_mode=(
            source.get(OPERATIONAL_CONSOLE_MODE_ENV) or DEFAULT_OPERATIONAL_CONSOLE_MODE
        ),
        gateway_event_path=Path(gateway_event_path_value) if gateway_event_path_value else None,
    )


def reset_operational_logging_for_tests() -> None:
    """Remove handlers owned by this module; intended for tests and reload setup."""

    global _GATEWAY_EVENT_STORE
    with _CONFIG_LOCK:
        _GATEWAY_EVENT_STORE = None
        for logger_name in (
            _PACKAGE_LOGGER_NAME,
            GATEWAY_LOGGER_NAME,
            _LEGACY_RUNTIME_LOGGER_NAME,
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

    return digest_gateway_identifier(value) or "-"


def record_gateway_lifecycle(event: GatewayLifecycleEvent) -> None:
    """Persist and log one prompt-safe Gateway lifecycle event."""

    append_gateway_lifecycle_event(event)
    log_gateway_lifecycle(event)


def append_gateway_lifecycle_event(event: GatewayLifecycleEvent) -> None:
    """Append one Gateway lifecycle event to the configured JSONL store."""

    try:
        with _CONFIG_LOCK:
            store = _GATEWAY_EVENT_STORE
        if store is not None:
            store.append(event)
    except Exception:  # noqa: BLE001 - lifecycle persistence is fail-open.
        return


def log_gateway_lifecycle(event: GatewayLifecycleEvent) -> None:
    """Project one prompt-safe Gateway lifecycle event to its component logger."""

    try:
        details = gateway_lifecycle_attributes(event.payload)
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


def _configure_gateway_event_store(
    gateway_event_path: Path | None,
    *,
    package_logger: logging.Logger,
) -> None:
    global _GATEWAY_EVENT_STORE
    _GATEWAY_EVENT_STORE = None
    if gateway_event_path is None:
        return
    try:
        _GATEWAY_EVENT_STORE = JsonlGatewayLifecycleStore(gateway_event_path)
    except OSError:
        package_logger.warning(
            "gateway event JSONL unavailable",
            extra={
                "component": "gateway",
                "event": "gateway.events_unavailable",
                "run_id": "-",
                "turn_id": "-",
                "trace_id": "-",
            },
        )


def _resolve_level(level: str) -> int:
    normalized = str(level).upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError(f"unsupported operational log level: {level}")
    return int(getattr(logging, normalized))


def _resolve_console_mode(mode: str) -> str:
    normalized = str(mode).lower()
    if normalized not in {"concise", "verbose"}:
        return DEFAULT_OPERATIONAL_CONSOLE_MODE
    return normalized


def _console_label(level_name: str, event: str) -> str:
    if event.endswith(".completed"):
        return "OK"
    if event.endswith(".cancelled"):
        return "CANCEL"
    if event.endswith(".errored") or event.endswith(".failed"):
        return "ERROR"
    return level_name


def _console_identifiers(record: logging.LogRecord) -> str:
    fields = (
        ("run", getattr(record, "run_id", "-")),
        ("turn", getattr(record, "turn_id", "-")),
        ("trace", getattr(record, "trace_id", "-")),
    )
    return " ".join(
        f"{name}={_short_identifier(str(value))}"
        for name, value in fields
        if value not in {None, "", "-"}
    )


def _short_identifier(value: str, *, limit: int = 16) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}…"


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


def _mark_handler(handler: logging.Handler, role: str) -> None:
    setattr(handler, _HANDLER_MARKER, True)
    setattr(handler, "_assistant_agent_operational_role", role)
