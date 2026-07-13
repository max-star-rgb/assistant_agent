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
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.config import ProviderConfig
from assistant_agent.services.assistant_run_service import load_env_file, runtime_info
from assistant_agent.services.provider_specs import (
    supported_chat_providers,
    supported_image_generation_providers,
)
from assistant_agent.services.trial_access import (
    TRIAL_USER_IDS_ENV,
    parse_trial_user_ids,
    trial_access_gate_from_env,
)


SKIP_DOTENV_ENV = "MULTIMODAL_AGENT_SKIP_DOTENV"
SERVER_TRACE_ENABLED_ENV = "MULTIMODAL_AGENT_SERVER_TRACE_ENABLED"
LOCAL_TRACE_CONTENT_ENV = "MULTIMODAL_AGENT_LOCAL_TRACE_CONTENT"


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
    os.environ[SERVER_TRACE_ENABLED_ENV] = "1"
    _configure_trial_user_allowlist(args)
    return loaded


def _allow_real_provider_if_needed(provider: str) -> None:
    if provider != "mock":
        os.environ.setdefault("MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke")


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


def _print_runtime_summary(config: ProviderConfig, *, loaded_env_keys: list[str]) -> None:
    info = runtime_info(config)
    providers = info["providers"]
    trial_access = trial_access_gate_from_env(base_dir=REPO_ROOT)
    print("Runtime configuration:")
    print(f"  env_file_keys_loaded: {len(loaded_env_keys)}")
    print(f"  runtime_profile: {info['runtime_profile']}")
    print(f"  graph_mode: {info['graph_mode']}")
    print(f"  chat_provider: {providers['chat']}")
    print(f"  chat_model: {config.chat_model or '(unset)'}")
    print(f"  vision_provider: {providers['vision']}")
    print(f"  image_provider: {providers['image_generation']}")
    print(f"  product_search_provider: {providers['product_search']}")
    print(f"  price_compare_provider: {providers['price_compare']}")
    print(f"  render_provider: {providers['render']}")
    print(f"  video_provider: {providers['video']}")
    print(f"  memory_backend: {config.memory_backend}")
    if config.memory_backend == "jsonl":
        print(f"  memory_path: {config.memory_path}")
    print(f"  conversation_history_backend: {config.conversation_history_backend}")
    if config.conversation_history_backend == "jsonl":
        print(f"  conversation_history_path: {config.conversation_history_path}")
    print(f"  langgraph_checkpointer_backend: {config.langgraph_checkpointer_backend}")
    print(
        "  local_trace_content: "
        + ("enabled" if os.environ.get(LOCAL_TRACE_CONTENT_ENV) == "1" else "disabled")
    )
    print(f"  offline_default: {info['offline_default']}")
    print(
        "  trial_access: "
        + (
            f"restricted ({trial_access.allowed_user_count} allowed user ids)"
            if trial_access.access_required
            else "open"
        )
    )
    missing = config.resolved_chat_provider().missing_required_env()
    if missing:
        print(f"  chat_provider_ready: no, missing {', '.join(missing)}")
    else:
        print("  chat_provider_ready: yes")
    _print_ignored_provider_hint(config)


def _print_ignored_provider_hint(config: ProviderConfig) -> None:
    selected = os.environ.get("MULTIMODAL_AGENT_CHAT_PROVIDER")
    if (
        selected
        and selected != "mock"
        and config.chat_provider == "mock"
        and not config.runtime_profile.allows_real_providers
    ):
        print(
            "  note: real chat provider selectors are ignored unless "
            "MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke or --provider is used."
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    loaded_env = _prepare_environment(args)
    config = ProviderConfig.from_env()

    import uvicorn

    # Surface assistant_agent INFO logs (e.g. WebSocket request/response lines).
    # uvicorn does not configure the root logger, so attach our own handler
    # instead of relying on propagation.
    pkg_logger = logging.getLogger("assistant_agent")
    pkg_logger.setLevel(logging.INFO)
    if not pkg_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        pkg_logger.addHandler(handler)
        pkg_logger.propagate = False

    base = f"http://{args.host}:{args.port}"
    print(f"Starting Assistant backend server on {base}")
    print(f"  Realtime media WS: {base}/ws/realtime/media")
    print(f"  Gateway WS: {base}/ws/gateway")
    print(
        "  Realtime smoke: "
        f"python scripts/realtime_media_client.py --server {base} --scenario basic"
    )
    print(f"  Gateway smoke: python scripts/run_gateway_client.py --server {base} \"你好\"")
    print(f"  access_log: {'enabled' if args.access_log else 'disabled'}")
    _print_runtime_summary(config, loaded_env_keys=sorted(loaded_env))
    if args.public_url:
        print(f"Share this URL with trial users: {args.public_url}")
    elif args.host in {"0.0.0.0", "::"}:
        print(f"Share realtime WS base: http://<your-machine-ip>:{args.port}")
    print("Press Ctrl+C to stop.")
    uvicorn.run(
        "assistant_agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=args.access_log,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
