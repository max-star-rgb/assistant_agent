#!/usr/bin/env python3
"""Start the assistant backend server (FastAPI + uvicorn).

This is the backend service for Gateway-first assistant entries. Realtime text
call smoke should use `/ws/realtime/media`; normalized Gateway smoke should use
`/ws/gateway`. The wrapper is intentionally thin so IDEs such as PyCharm can
launch it without a module-based uvicorn run configuration.
"""

# ruff: noqa: E402 - repository src path must be installed before package imports.

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.config import ProviderConfig
from assistant_agent.gateway.observability import GatewayLifecycleEvent
from assistant_agent.services.assistant_run_service import load_env_file
from assistant_agent.services.operational_logging import (
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
from assistant_agent.schemas.provider_specs import (
    supported_chat_providers,
    supported_image_generation_providers,
)
from assistant_agent.services.tool_workflow_skill_runtime_app import (
    DEFAULT_WORKFLOW_SKILL_MANIFEST_DIR,
    DEFAULT_WORKFLOW_SKILL_RUN_STORE,
    WORKFLOW_SKILLS_ENABLED_ENV,
    WORKFLOW_SKILL_MANIFEST_DIR_ENV,
    WORKFLOW_SKILL_RUN_STORE_ENV,
    WORKFLOW_SKILL_TOOL_MODULES_ENV,
)
from assistant_agent.services.trial_access import (
    TRIAL_USER_IDS_ENV,
    parse_trial_user_ids,
)


SKIP_DOTENV_ENV = "MULTIMODAL_AGENT_SKIP_DOTENV"
SERVER_TRACE_ENABLED_ENV = "MULTIMODAL_AGENT_SERVER_TRACE_ENABLED"
LOCAL_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"
START_WEB_SEARCH_RELAY_ENV = "MULTIMODAL_AGENT_START_WEB_SEARCH_RELAY"
PROVIDER_MODE_ENV = "MULTIMODAL_AGENT_PROVIDER_MODE"
SEARCH_PROVIDER_ENV = "MULTIMODAL_AGENT_SEARCH_PROVIDER"
WEB_SEARCH_BASE_URL_ENV = "WEB_SEARCH_BASE_URL"
WEB_SEARCH_API_KEY_ENV = "WEB_SEARCH_API_KEY"
WEB_SEARCH_RELAY_API_KEY_ENV = "WEB_SEARCH_RELAY_API_KEY"
TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
DEFAULT_WEB_SEARCH_RELAY_HOST = "127.0.0.1"
DEFAULT_WEB_SEARCH_RELAY_PORT = 7005
DEFAULT_WEB_SEARCH_RELAY_PATH = "/search"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the assistant backend server (FastAPI).")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    parser.add_argument("--public-url", default=None, help="Optional URL to print for sharing with beta users.")
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
        "--enable-workflow-skills",
        action="store_true",
        help="Enable explicit workflow skill HTTP APIs for this process.",
    )
    parser.add_argument(
        "--workflow-skill-manifest-dir",
        default=DEFAULT_WORKFLOW_SKILL_MANIFEST_DIR,
        help="Directory containing workflow_skill_v1 JSON manifests.",
    )
    parser.add_argument(
        "--workflow-skill-tool-module",
        action="append",
        default=[],
        help="Python module exposing __assistant_tools__ for workflow skills. Repeatable.",
    )
    parser.add_argument(
        "--workflow-skill-run-store",
        default=DEFAULT_WORKFLOW_SKILL_RUN_STORE,
        help="JSONL path used to persist workflow skill run records.",
    )
    parser.add_argument(
        "--start-web-search-relay",
        action="store_true",
        help=(
            "Start the local Tavily web_search relay as a child process for "
            "developer runs. Can also be enabled with "
            f"{START_WEB_SEARCH_RELAY_ENV}=1."
        ),
    )
    parser.add_argument(
        "--web-search-relay-host",
        default=DEFAULT_WEB_SEARCH_RELAY_HOST,
        help="Host for the local web_search relay when --start-web-search-relay is used.",
    )
    parser.add_argument(
        "--web-search-relay-port",
        type=int,
        default=DEFAULT_WEB_SEARCH_RELAY_PORT,
        help="Port for the local web_search relay when --start-web-search-relay is used.",
    )
    parser.add_argument(
        "--web-search-relay-path",
        default=DEFAULT_WEB_SEARCH_RELAY_PATH,
        help="Path for the local web_search relay when --start-web-search-relay is used.",
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
    if args.enable_workflow_skills:
        os.environ[WORKFLOW_SKILLS_ENABLED_ENV] = "1"
    os.environ[WORKFLOW_SKILL_MANIFEST_DIR_ENV] = args.workflow_skill_manifest_dir
    os.environ[WORKFLOW_SKILL_RUN_STORE_ENV] = args.workflow_skill_run_store
    if args.workflow_skill_tool_module:
        os.environ[WORKFLOW_SKILL_TOOL_MODULES_ENV] = ",".join(args.workflow_skill_tool_module)
    if _should_start_web_search_relay(args):
        _prepare_web_search_relay_environment(args)
    os.environ[SERVER_TRACE_ENABLED_ENV] = "1"
    _configure_trial_user_allowlist(args)
    return loaded


def _should_start_web_search_relay(args: argparse.Namespace) -> bool:
    return args.start_web_search_relay or os.environ.get(START_WEB_SEARCH_RELAY_ENV) == "1"


def _prepare_web_search_relay_environment(args: argparse.Namespace) -> None:
    """Configure agent and relay env for explicit local Tavily relay dev runs."""

    if not os.environ.get(TAVILY_API_KEY_ENV):
        raise RuntimeError(
            "--start-web-search-relay requires TAVILY_API_KEY in the environment or env file."
        )

    relay_url = _web_search_relay_url(args)
    relay_secret = (
        os.environ.get(WEB_SEARCH_RELAY_API_KEY_ENV)
        or os.environ.get(WEB_SEARCH_API_KEY_ENV)
        or _generate_relay_secret()
    )
    os.environ[WEB_SEARCH_RELAY_API_KEY_ENV] = relay_secret
    os.environ[WEB_SEARCH_API_KEY_ENV] = relay_secret
    os.environ[WEB_SEARCH_BASE_URL_ENV] = relay_url
    if os.environ.get(PROVIDER_MODE_ENV, "mock") != "real":
        raise RuntimeError(
            "web_search relay requires MULTIMODAL_AGENT_PROVIDER_MODE=real"
        )
    os.environ[SEARCH_PROVIDER_ENV] = "http"


def _generate_relay_secret() -> str:
    return secrets.token_urlsafe(24)


def _web_search_relay_url(args: argparse.Namespace) -> str:
    path = _normalize_relay_path(args.web_search_relay_path)
    return f"http://{args.web_search_relay_host}:{args.web_search_relay_port}{path}"


def _normalize_relay_path(value: str) -> str:
    stripped = value.strip() or DEFAULT_WEB_SEARCH_RELAY_PATH
    return stripped if stripped.startswith("/") else f"/{stripped}"


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


def _print_startup_summary(
    args: argparse.Namespace,
    config: ProviderConfig,
    *,
    web_search_relay_started: bool,
) -> None:
    ws_base = f"ws://{args.host}:{args.port}"
    model = config.chat_model or "default"

    print(f"Provider mode: {config.provider_mode}")
    print(f"Main LLM: {config.chat_provider} / {model}")
    print("Services:")
    print(f"  media: {ws_base}/ws/realtime/media")
    relay = _web_search_relay_url(args) if web_search_relay_started else "disabled"
    print(f"  web_search_relay: {relay}")
    print("Mock tools:")
    mock_tools = _mock_tool_labels(config)
    if mock_tools:
        for label in mock_tools:
            print(f"  {label}")
    else:
        print("  none")
    print("Tools:")
    loaded_tools = _loaded_non_mock_tool_labels(config)
    if loaded_tools:
        for label in loaded_tools:
            print(f"  {label}")
    else:
        print("  none")


def _mock_tool_labels(config: ProviderConfig) -> list[str]:
    if config.provider_mode != "mock":
        return []
    labels: list[str] = []
    if config.vision_provider == "mock":
        labels.append("vision_understanding")
    if config.image_generation_provider == "mock":
        labels.append("image_generation")
    if config.search_provider == "mock":
        labels.append("web_search, web_fetch")
    if config.visual_image_search_provider == "mock":
        labels.append("visual_image_search")
    if (
        config.shopping_search_provider == "mock"
        and config.shopping_compare_provider == "mock"
    ):
        labels.append("shopping_search")
    return labels


def _loaded_non_mock_tool_labels(config: ProviderConfig) -> list[str]:
    if config.provider_mode != "real":
        return []
    labels: list[str] = []
    if (
        config.vision_provider != "mock"
        and not config.resolved_vision_provider().missing_required_env()
    ):
        labels.append("vision_understanding")
    if (
        config.image_generation_provider != "mock"
        and not config.resolved_image_generation_provider().missing_required_env()
    ):
        labels.append("image_generation")
    if (
        config.search_provider == "http"
        and config.web_search_base_url
        and config.web_search_api_key
    ):
        labels.append("web_search, web_fetch")
    if (
        config.visual_image_search_provider == "qwen"
        and config.qwen_image_search_api_key
    ):
        labels.append("visual_image_search")
    if _shopping_provider_is_loaded(config):
        labels.append("shopping_search")
    return labels


def _shopping_provider_is_loaded(config: ProviderConfig) -> bool:
    search_provider = config.shopping_search_provider
    compare_provider = config.shopping_compare_provider
    if search_provider == "haodanku" and compare_provider == "haodanku":
        return bool(config.haodanku_api_key)
    if search_provider == "http" and compare_provider == "http":
        return bool(
            config.shopping_search_base_url
            and config.shopping_search_api_key
            and config.shopping_compare_base_url
            and config.shopping_compare_api_key
        )
    return False


@contextmanager
def _web_search_relay_process(args: argparse.Namespace) -> Iterator[subprocess.Popen[str] | None]:
    if not _should_start_web_search_relay(args):
        yield None
        return

    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_tavily_search_relay.py"),
        "--host",
        args.web_search_relay_host,
        "--port",
        str(args.web_search_relay_port),
        "--path",
        _normalize_relay_path(args.web_search_relay_path),
        "--quiet",
    ]
    process = subprocess.Popen(command, cwd=REPO_ROOT, env=os.environ.copy(), text=True)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _prepare_environment(args)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    config = ProviderConfig.from_env()

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
    configure_operational_logging_from_env()
    _log_gateway_server_starting(args, log_dir=log_dir, gateway_event_path=gateway_event_path)

    import uvicorn

    with _web_search_relay_process(args) as relay_process:
        _print_startup_summary(
            args,
            config,
            web_search_relay_started=relay_process is not None,
        )
        uvicorn.run(
            "assistant_agent.api.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            access_log=args.access_log,
            log_level="info" if args.access_log else "warning",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
