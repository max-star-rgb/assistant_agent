#!/usr/bin/env python3
"""Start the assistant backend server (FastAPI + uvicorn).

This is the backend service for Gateway-first assistant entries. Media-Agent
integration should use `/agent-service/v1`; normalized Gateway smoke should use
`/ws/gateway`. The wrapper is intentionally thin so IDEs such as PyCharm can
launch it without a module-based uvicorn run configuration.
"""

# ruff: noqa: E402 - repository src path must be installed before package imports.

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import uvicorn

from assistant_agent.gateway.observability import GatewayLifecycleEvent
from assistant_agent.runtime.assistant_run_service import load_env_file
from assistant_agent.observability.operational_logging import (
    DEFAULT_GATEWAY_EVENT_PATH,
    GATEWAY_EVENT_PATH_ENV,
    OPERATIONAL_CONSOLE_LEVEL_ENV,
    OPERATIONAL_CONSOLE_MODE_ENV,
    OPERATIONAL_FILE_LEVEL_ENV,
    OPERATIONAL_LOG_DIR_ENV,
    OPERATIONAL_LOG_LEVEL_ENV,
    OPERATIONAL_LOGGING_ENABLED_ENV,
    configure_operational_logging_from_env,
    record_gateway_lifecycle,
)
from assistant_agent.runtime.server_startup_summary import (
    STARTUP_BIND_HOST_ENV,
    STARTUP_BIND_PORT_ENV,
    STARTUP_DETAILS_ENV,
    STARTUP_PUBLIC_URL_ENV,
    print_prepared_server_startup_report,
)
from assistant_agent.providers.specs import (
    supported_chat_providers,
    supported_image_generation_providers,
)
from assistant_agent.skills.application import (
    DEFAULT_SKILL_MANIFEST_DIR,
    DEFAULT_SKILL_RUN_STORE,
    SKILLS_ENABLED_ENV,
    SKILL_MANIFEST_DIR_ENV,
    SKILL_RUN_STORE_ENV,
    SKILL_TOOL_MODULES_ENV,
)
from assistant_agent.api.trial_access import (
    TRIAL_USER_IDS_ENV,
    parse_trial_user_ids,
)


SKIP_DOTENV_ENV = "MULTIMODAL_AGENT_SKIP_DOTENV"
SERVER_TRACE_ENABLED_ENV = "MULTIMODAL_AGENT_SERVER_TRACE_ENABLED"
LOCAL_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"
LOCAL_PROVIDER_PROTOCOL_CAPTURE_ENV = "MULTIMODAL_AGENT_LOCAL_PROVIDER_PROTOCOL_CAPTURE"
PROVIDER_MODE_ENV = "MULTIMODAL_AGENT_PROVIDER_MODE"


class StartupReportingServer(uvicorn.Server):
    """Uvicorn server that prints the prepared report after binding succeeds."""

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            print_prepared_server_startup_report()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the assistant backend server (FastAPI).")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    parser.add_argument("--public-url", default=None, help="Optional URL to print for sharing with beta users.")
    parser.add_argument(
        "--startup-details",
        action="store_true",
        help="Include the full Tool ownership inventory after the compact startup report.",
    )
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload for local development.")
    parser.add_argument(
        "--access-log",
        action="store_true",
        help="Enable uvicorn per-request access logs. Disabled by default for a quieter dev console.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None,
        help="Legacy shorthand that sets both console and file log levels.",
    )
    parser.add_argument(
        "--console-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Minimum level shown in the Combined console (default: INFO).",
    )
    parser.add_argument(
        "--file-log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="DEBUG",
        help="Minimum level written to gateway.log (default: DEBUG).",
    )
    parser.add_argument(
        "--console-mode",
        choices=("concise", "verbose"),
        default="concise",
        help="Concise lifecycle summary or all prompt-safe operational events.",
    )
    parser.add_argument(
        "--log-dir",
        default=".data/logs",
        help="Directory for rotating gateway.log.",
    )
    parser.add_argument(
        "--gateway-event-path",
        default=str(DEFAULT_GATEWAY_EVENT_PATH),
        help=f"Path for Gateway lifecycle JSONL events. Defaults to {DEFAULT_GATEWAY_EVENT_PATH}.",
    )
    parser.add_argument(
        "--allow-local-trace-content",
        action="store_true",
        help="Allow explicit trace conversation lookup from loopback clients only.",
    )
    parser.add_argument(
        "--allow-local-provider-protocol-capture",
        action="store_true",
        help="Capture selected Provider protocol fields for local diagnostics.",
    )
    parser.add_argument("--env-file", default=".env", help="Env file to load before starting.")
    parser.add_argument("--no-env-file", action="store_true", help="Do not load a dotenv file before starting.")
    parser.add_argument(
        "--trial-user-id",
        action="append",
        default=[],
        help="Allowed realtime/Gateway trial user id(s). Repeat or comma-separate to allow multiple ids.",
    )
    parser.add_argument(
        "--trial-user-id-file",
        default=None,
        help="Text file of allowed realtime/Gateway trial user ids, one per line or comma separated.",
    )
    parser.add_argument(
        "--provider",
        choices=supported_chat_providers(),
        help="Override MULTIMODAL_AGENT_CHAT_PROVIDER for this process.",
    )
    parser.add_argument(
        "--image-provider",
        choices=supported_image_generation_providers(),
        help="Override MULTIMODAL_AGENT_IMAGE_PROVIDER for this process.",
    )
    parser.add_argument(
        "--enable-skills",
        action="store_true",
        help="Enable explicit skill HTTP APIs for this process.",
    )
    parser.add_argument(
        "--skill-manifest-dir",
        default=DEFAULT_SKILL_MANIFEST_DIR,
        help="Directory containing skill_v1 JSON manifests.",
    )
    parser.add_argument(
        "--skill-tool-module",
        action="append",
        default=[],
        help="Python module exposing __assistant_tools__ for skills. Repeatable.",
    )
    parser.add_argument(
        "--skill-run-store",
        default=DEFAULT_SKILL_RUN_STORE,
        help="JSONL path used to persist skill run records.",
    )
    return parser


def _prepare_environment(args: argparse.Namespace) -> dict[str, str]:
    """Load local env config and apply explicit one-run provider overrides."""

    loaded: dict[str, str] = {}
    if args.no_env_file:
        os.environ[SKIP_DOTENV_ENV] = "1"
    else:
        loaded = load_env_file((REPO_ROOT / args.env_file).resolve())

    if args.provider:
        _allow_real_provider_if_needed(args.provider)
        os.environ["MULTIMODAL_AGENT_CHAT_PROVIDER"] = args.provider
    if args.image_provider:
        _allow_real_provider_if_needed(args.image_provider)
        os.environ["MULTIMODAL_AGENT_IMAGE_PROVIDER"] = args.image_provider
    if args.allow_local_trace_content:
        os.environ[LOCAL_TRACE_CONTENT_ENV] = "1"
    if args.allow_local_provider_protocol_capture:
        os.environ[LOCAL_PROVIDER_PROTOCOL_CAPTURE_ENV] = "1"
    if args.enable_skills:
        os.environ[SKILLS_ENABLED_ENV] = "1"
    os.environ[SKILL_MANIFEST_DIR_ENV] = args.skill_manifest_dir
    os.environ[SKILL_RUN_STORE_ENV] = args.skill_run_store
    if args.skill_tool_module:
        os.environ[SKILL_TOOL_MODULES_ENV] = ",".join(args.skill_tool_module)
    os.environ[SERVER_TRACE_ENABLED_ENV] = "1"
    _configure_trial_user_allowlist(args)
    return loaded


def _allow_real_provider_if_needed(provider: str) -> None:
    if provider != "mock":
        os.environ[PROVIDER_MODE_ENV] = "real"


def _configure_trial_user_allowlist(args: argparse.Namespace) -> None:
    ids = _trial_user_ids_from_args(args)
    if ids:
        existing = parse_trial_user_ids(os.environ.get(TRIAL_USER_IDS_ENV))
        combined = sorted({*existing, *ids})
        os.environ[TRIAL_USER_IDS_ENV] = ",".join(combined)


def _trial_user_ids_from_args(args: argparse.Namespace) -> list[str]:
    ids: set[str] = set()
    for value in args.trial_user_id:
        ids.update(parse_trial_user_ids(value))
    if args.trial_user_id_file:
        path = Path(args.trial_user_id_file).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        if not path.exists():
            raise FileNotFoundError(f"--trial-user-id-file does not exist: {path}")
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            ids.update(parse_trial_user_ids(line))
    return sorted(item for item in ids if item)


def _log_gateway_server_starting(
    args: argparse.Namespace,
    *,
    log_dir: Path,
    gateway_event_path: Path,
) -> None:
    record_gateway_lifecycle(
        GatewayLifecycleEvent(
            type="gateway.server.starting",
            payload={
                "host": args.host,
                "port": args.port,
                "log_dir": str(log_dir),
                "gateway_event_path": str(gateway_event_path),
            },
        )
    )


def _run_uvicorn(args: argparse.Namespace) -> None:
    """Run Uvicorn and print READY only after its listener has been created."""

    from uvicorn.supervisors import ChangeReload

    config = uvicorn.Config(
        "assistant_agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=args.access_log,
        log_level="info" if args.access_log else "warning",
    )
    config.load_app()
    server = StartupReportingServer(config=config)
    try:
        if config.should_reload:
            socket = config.bind_socket()
            ChangeReload(config, target=server.run, sockets=[socket]).run()
            return
        server.run()
    except KeyboardInterrupt:
        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _prepare_environment(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    log_dir = Path(args.log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = REPO_ROOT / log_dir
    gateway_event_path = Path(args.gateway_event_path).expanduser()
    if not gateway_event_path.is_absolute():
        gateway_event_path = REPO_ROOT / gateway_event_path
    os.environ[OPERATIONAL_LOGGING_ENABLED_ENV] = "1"
    os.environ[OPERATIONAL_LOG_DIR_ENV] = str(log_dir)
    os.environ[GATEWAY_EVENT_PATH_ENV] = str(gateway_event_path)
    if args.log_level is not None:
        os.environ[OPERATIONAL_LOG_LEVEL_ENV] = args.log_level
    else:
        os.environ.pop(OPERATIONAL_LOG_LEVEL_ENV, None)
    os.environ[OPERATIONAL_CONSOLE_LEVEL_ENV] = args.console_level
    os.environ[OPERATIONAL_FILE_LEVEL_ENV] = args.file_log_level
    os.environ[OPERATIONAL_CONSOLE_MODE_ENV] = args.console_mode
    os.environ[STARTUP_BIND_HOST_ENV] = args.host
    os.environ[STARTUP_BIND_PORT_ENV] = str(args.port)
    if args.public_url:
        os.environ[STARTUP_PUBLIC_URL_ENV] = args.public_url
    else:
        os.environ.pop(STARTUP_PUBLIC_URL_ENV, None)
    if args.startup_details:
        os.environ[STARTUP_DETAILS_ENV] = "1"
    else:
        os.environ.pop(STARTUP_DETAILS_ENV, None)
    configure_operational_logging_from_env()
    _log_gateway_server_starting(args, log_dir=log_dir, gateway_event_path=gateway_event_path)

    print(f"assistant_agent  STARTING ({args.host}:{args.port})", flush=True)
    _run_uvicorn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
