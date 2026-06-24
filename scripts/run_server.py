#!/usr/bin/env python3
"""Start the assistant backend server (FastAPI + uvicorn).

This is the single backend service that both clients talk to: the Web Console
at `/demo/console` and the CLI client (`scripts/run_client.py`). It owns all
provider/env configuration. The wrapper is intentionally thin so IDEs such as
PyCharm can launch it without a module-based uvicorn run configuration.
"""

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

from multimodal_agent.config import ProviderConfig
from multimodal_agent.services.assistant_run_service import load_env_file, runtime_info
from multimodal_agent.services.provider_specs import (
    supported_chat_providers,
    supported_image_generation_providers,
)


SKIP_DOTENV_ENV = "MULTIMODAL_AGENT_SKIP_DOTENV"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the assistant backend server (FastAPI).")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    parser.add_argument("--public-url", default=None, help="Optional URL to print for sharing with beta users.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload for local development.")
    parser.add_argument("--env-file", default=".env", help="Env file to load before starting.")
    parser.add_argument("--no-env-file", action="store_true", help="Do not load a dotenv file before starting.")
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
    return loaded


def _allow_real_provider_if_needed(provider: str) -> None:
    if provider != "mock":
        os.environ.setdefault("MULTIMODAL_AGENT_RUNTIME_PROFILE", "provider_smoke")


def _print_runtime_summary(config: ProviderConfig, *, loaded_env_keys: list[str]) -> None:
    info = runtime_info(config)
    providers = info["providers"]
    print("Runtime configuration:")
    print(f"  env_file_keys_loaded: {len(loaded_env_keys)}")
    print(f"  runtime_profile: {info['runtime_profile']}")
    print(f"  graph_mode: {info['graph_mode']}")
    print(f"  chat_provider: {providers['chat']}")
    print(f"  chat_model: {config.chat_model or '(unset)'}")
    print(f"  image_provider: {providers['image_generation']}")
    print(f"  video_provider: {providers['video']}")
    print(f"  offline_default: {info['offline_default']}")
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

    base = f"http://{args.host}:{args.port}"
    url = f"{base}/demo/console"
    print(f"Starting Assistant backend server on {base}")
    print(f"  Web Console (browser client): {url}")
    print(f"  CLI client: python scripts/run_client.py --server {base} \"你好\"")
    _print_runtime_summary(config, loaded_env_keys=sorted(loaded_env))
    if args.public_url:
        print(f"Share this URL with trial users: {args.public_url}")
    elif args.host in {"0.0.0.0", "::"}:
        print(f"Share URL format: http://<your-machine-ip>:{args.port}/demo/console")
    print("Press Ctrl+C to stop.")
    uvicorn.run(
        "multimodal_agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
