#!/usr/bin/env python3
"""Check local Agent Control Plane pilot readiness without starting the server."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.api.auth import require_auth_bound_identity, resolve_auth_mode
from assistant_agent.config import ProviderConfig
from assistant_agent.schemas.agent_communication import AgentInstance
from assistant_agent.services.agent_directory import AgentDirectory, default_agent_instance
from assistant_agent.services.agent_pilot_readiness import PilotReadinessChecker
from assistant_agent.services.api_identity import AuthContext, IdentityPolicy, resolve_request_identity
from assistant_agent.services.provider_errors import sanitize_error_detail
from assistant_agent.services.provider_readiness import build_provider_readiness_report


REMOTE_ALLOWLIST_ENV = "MULTIMODAL_AGENT_REMOTE_A2A_ALLOWLIST"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check pilot readiness gates without provider or remote-agent calls.",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Request user id to evaluate. Defaults to auth user or pilot_check.",
    )
    parser.add_argument("--session-id", default="pilot_check_session", help="Request session id to evaluate.")
    parser.add_argument("--auth-user-id", default=None, help="Simulate an auth-bound user id for the check.")
    parser.add_argument("--auth-session-id", default=None, help="Simulate an auth-bound session id for the check.")
    parser.add_argument(
        "--require-auth-bound-identity",
        action="store_true",
        help="Require auth-bound identity for this check regardless of environment.",
    )
    parser.add_argument(
        "--remote-agent",
        action="append",
        default=[],
        metavar="AGENT_ID=URL",
        help="Add an explicitly configured remote A2A agent to validate.",
    )
    parser.add_argument(
        "--allowlisted-host",
        action="append",
        default=[],
        help="Allowlisted remote host. Repeat or comma-separate. Also reads MULTIMODAL_AGENT_REMOTE_A2A_ALLOWLIST.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless the final status is exactly ready.",
    )
    return parser


def build_report(args: argparse.Namespace) -> dict[str, object]:
    config = ProviderConfig.from_env()
    request_user_id = args.user_id or args.auth_user_id or "pilot_check"
    auth_context = _auth_context(args)
    identity = resolve_request_identity(
        user_id=request_user_id,
        session_id=args.session_id,
        source="local_context",
        auth_context=auth_context,
    )
    production_required = args.require_auth_bound_identity or require_auth_bound_identity()
    identity_policy = IdentityPolicy().evaluate(identity, production_required=production_required)
    report = PilotReadinessChecker().evaluate(
        directory=_directory_from_remote_agents(args.remote_agent),
        provider_mode=config.provider_mode,
        allowlisted_hosts=_allowlisted_hosts(args.allowlisted_host),
        identity_policy=identity_policy,
        provider_readiness=build_provider_readiness_report(config),
    )
    payload = report.model_dump(mode="json")
    payload["operator_context"] = sanitize_error_detail(
        {
            "auth_mode": resolve_auth_mode(),
            "auth_bound_identity_required": production_required,
            "auth_bound_identity_supplied": identity.auth_bound,
            "provider_mode": config.provider_mode,
            "real_provider_allowed": config.provider_mode == "real",
            "remote_agent_count": len(args.remote_agent),
        }
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_report(args)
    except ValueError as exc:
        payload = {
            "schema_version": "agent_pilot_readiness_v1",
            "status": "blocked",
            "checks": [
                {
                    "name": "pilot_readiness_input",
                    "status": "failed",
                    "detail": sanitize_error_detail({"error": str(exc)}),
                }
            ],
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.strict:
        return 0 if payload.get("status") == "ready" else 1
    return 0 if payload.get("status") != "blocked" else 1


def _auth_context(args: argparse.Namespace) -> AuthContext:
    if not args.auth_user_id:
        return AuthContext.anonymous()
    return AuthContext(
        authenticated=True,
        source="header",
        user_id=args.auth_user_id,
        session_id=args.auth_session_id,
    )


def _directory_from_remote_agents(remote_agents: list[str]) -> AgentDirectory | None:
    if not remote_agents:
        return None
    instances = [default_agent_instance()]
    for spec in remote_agents:
        agent_id, endpoint_url = _remote_agent_spec(spec)
        instances.append(
            AgentInstance(
                agent_id=agent_id,
                display_name=agent_id,
                transports=["a2a_json_rpc"],
                endpoint_url=endpoint_url,
            )
        )
    return AgentDirectory(instances)


def _remote_agent_spec(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError("--remote-agent must use AGENT_ID=URL")
    agent_id, endpoint_url = (part.strip() for part in value.split("=", 1))
    if not agent_id or not endpoint_url:
        raise ValueError("--remote-agent must include both AGENT_ID and URL")
    return agent_id, endpoint_url


def _allowlisted_hosts(values: list[str]) -> list[str]:
    hosts: list[str] = []
    for source in [os.environ.get(REMOTE_ALLOWLIST_ENV, ""), *values]:
        hosts.extend(_split_csv(source))
    return sorted(dict.fromkeys(hosts))


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(main())
