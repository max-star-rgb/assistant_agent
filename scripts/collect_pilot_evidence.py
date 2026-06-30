#!/usr/bin/env python3
"""Collect a redacted local pilot evidence package.

The default mode is intentionally offline and same-process:

- no server startup;
- no dotenv loading;
- mock/local providers only;
- header-bound identity enabled for the in-process API calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

AUTH_USER_ID_HEADER = "X-Multimodal-Agent-User-Id"
AUTH_SESSION_ID_HEADER = "X-Multimodal-Agent-Session-Id"
SENSITIVE_KEY_REPLACEMENT = "[redacted]"
OFFLINE_ENV_DEFAULTS = {
    "MULTIMODAL_AGENT_SKIP_DOTENV": "1",
    "MULTIMODAL_AGENT_RUNTIME_PROFILE": "local_demo",
    "MULTIMODAL_AGENT_AUTH_MODE": "header_pilot",
    "MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY": "true",
    "MULTIMODAL_AGENT_CHAT_PROVIDER": "mock",
    "MULTIMODAL_AGENT_VISION_PROVIDER": "mock",
    "MULTIMODAL_AGENT_IMAGE_PROVIDER": "mock",
    "MULTIMODAL_AGENT_PRODUCT_PROVIDER": "mock",
    "MULTIMODAL_AGENT_PRICE_PROVIDER": "mock",
    "MULTIMODAL_AGENT_RENDER_PROVIDER": "mock",
    "MULTIMODAL_AGENT_VIDEO_PROVIDER": "mock",
}
SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "password",
    "raw_provider_response",
    "secret",
    "signature",
    "token",
)
SENSITIVE_TEXT_MARKERS = ("sk-", "Bearer ", "raw provider payload", "raw_provider_response")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a redacted Agent Control Plane pilot evidence package.",
    )
    parser.add_argument("--user-id", default="pilot_01", help="Pilot user id to bind to request headers.")
    parser.add_argument("--session-id", default="pilot_evidence_session", help="Pilot session id to bind.")
    parser.add_argument(
        "--use-current-env",
        action="store_true",
        help="Use the current process environment instead of forcing local_demo/mock/header_pilot defaults.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output path. Stdout is always used when this is omitted.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless all evidence gates pass.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.use_current_env:
        _apply_offline_env_defaults()

    payload = collect_evidence(user_id=args.user_id, session_id=args.session_id)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output_path = _output_path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)

    if args.strict:
        return 0 if payload.get("status") == "passed" else 1
    return 0 if payload.get("status") != "failed" else 1


def collect_evidence(*, user_id: str, session_id: str) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from multimodal_agent.api import routes_agent
    from multimodal_agent.api.app import create_app

    routes_agent._RUNTIME = None
    routes_agent._AGENT_GATEWAY = None
    client = TestClient(create_app())
    headers = {
        "content-type": "application/json",
        AUTH_USER_ID_HEADER: user_id,
        AUTH_SESSION_ID_HEADER: session_id,
    }

    readiness = _request(client, "GET", "/control-plane/readiness", headers=headers)
    agent_card = _request(client, "GET", "/.well-known/agent-card.json", headers=headers)
    single = _single_agent_evidence(client, headers=headers, user_id=user_id, session_id=session_id)
    gateway = _gateway_evidence(client, headers=headers, user_id=user_id, session_id=session_id)
    a2a = _a2a_evidence(client, headers=headers, user_id=user_id, session_id=session_id)
    recent_audit = _request(client, "GET", "/control-plane/audit/events?limit=20", headers=headers)

    payload: dict[str, Any] = {
        "schema_version": "agent_pilot_evidence_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operator_context": _operator_context(user_id=user_id, session_id=session_id),
        "readiness": readiness,
        "agent_card": _agent_card_summary(agent_card),
        "single_agent_run": single,
        "gateway_run": gateway,
        "a2a_inbound_run": a2a,
        "control_plane_recent_audit": recent_audit,
    }
    payload["redaction_checks"] = _redaction_checks(payload)
    payload["status"] = _overall_status(payload)
    return payload


def _single_agent_evidence(client: Any, *, headers: Mapping[str, str], user_id: str, session_id: str) -> dict[str, Any]:
    response = _request(
        client,
        "POST",
        "/agent/run",
        headers=headers,
        json={
            "user_id": user_id,
            "session_id": session_id,
            "text": "pilot evidence single-agent smoke",
            "metadata": {"source": "pilot_evidence", "entrypoint": "agent_run"},
        },
    )
    run_id = _body_value(response, "run_id")
    trace_id = _body_value(response, "trace_id")
    return {
        "entrypoint": "/agent/run",
        "response": _run_response_summary(response),
        "control_plane": _collect_control_plane(client, headers=headers, run_id=run_id, trace_id=trace_id, gateway=False),
    }


def _gateway_evidence(client: Any, *, headers: Mapping[str, str], user_id: str, session_id: str) -> dict[str, Any]:
    response = _request(
        client,
        "POST",
        "/agents/run",
        headers=headers,
        json={
            "user_id": user_id,
            "session_id": session_id,
            "text": "pilot evidence gateway smoke",
            "collaboration_mode": "single",
            "target_agent_id": "agent.worker",
            "metadata": {"source": "pilot_evidence", "entrypoint": "agents_run"},
        },
    )
    run_id = _body_value(response, "run_id")
    trace_id = _body_value(response, "trace_id")
    return {
        "entrypoint": "/agents/run",
        "response": _run_response_summary(response),
        "control_plane": _collect_control_plane(client, headers=headers, run_id=run_id, trace_id=trace_id, gateway=True),
    }


def _a2a_evidence(client: Any, *, headers: Mapping[str, str], user_id: str, session_id: str) -> dict[str, Any]:
    response = _request(
        client,
        "POST",
        "/a2a/rpc",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "pilot-evidence-a2a",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "pilot-evidence-message",
                    "contextId": session_id,
                    "parts": [{"kind": "text", "text": "pilot evidence inbound A2A smoke"}],
                    "metadata": {
                        "user_id": user_id,
                        "session_id": session_id,
                        "target_agent_id": "agent.worker",
                    },
                }
            },
        },
    )
    result = response.get("body", {}).get("result") if isinstance(response.get("body"), dict) else None
    run_id = result.get("id") if isinstance(result, dict) else None
    trace_id = result.get("metadata", {}).get("trace_id") if isinstance(result, dict) else None
    return {
        "entrypoint": "/a2a/rpc",
        "response": {
            "ok": response["ok"],
            "status_code": response["status_code"],
            "jsonrpc_error": _safe_value(response.get("body", {}).get("error")) if isinstance(response.get("body"), dict) else None,
            "task": _a2a_task_summary(result),
        },
        "control_plane": _collect_control_plane(client, headers=headers, run_id=run_id, trace_id=trace_id, gateway=True),
    }


def _collect_control_plane(
    client: Any,
    *,
    headers: Mapping[str, str],
    run_id: str | None,
    trace_id: str | None,
    gateway: bool,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    if run_id:
        evidence["run"] = _request(client, "GET", f"/control-plane/runs/{run_id}", headers=headers)
        evidence["budget"] = _request(client, "GET", f"/control-plane/runs/{run_id}/budget", headers=headers)
        if gateway:
            evidence["route"] = _request(client, "GET", f"/control-plane/runs/{run_id}/route", headers=headers)
            evidence["delegation_tree"] = _request(
                client,
                "GET",
                f"/control-plane/runs/{run_id}/delegation-tree",
                headers=headers,
            )
            evidence["audit"] = _request(client, "GET", f"/control-plane/runs/{run_id}/audit", headers=headers)
            evidence["replay_preview"] = _request(
                client,
                "GET",
                f"/control-plane/runs/{run_id}/replay-preview",
                headers=headers,
            )
    if trace_id:
        evidence["trace"] = _request(client, "GET", f"/control-plane/traces/{trace_id}", headers=headers)
    return evidence


def _request(
    client: Any,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, url, headers=dict(headers or {}), json=json)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
    return {
        "ok": 200 <= response.status_code < 300,
        "status_code": response.status_code,
        "body": _compact_response_body(url, _safe_value(body)),
    }


def _run_response_summary(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    errors = body.get("errors") if isinstance(body, dict) else []
    data = body.get("data") if isinstance(body, dict) else {}
    gateway = data.get("agent_gateway", {}) if isinstance(data, dict) else {}
    return {
        "ok": response["ok"],
        "status_code": response["status_code"],
        "run_id": body.get("run_id"),
        "trace_id": body.get("trace_id"),
        "status": body.get("status"),
        "intent": body.get("intent"),
        "error_count": len(errors or []),
        "agent_gateway": _safe_value(gateway),
    }


def _a2a_task_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    status = result.get("status", {})
    metadata = result.get("metadata", {})
    return {
        "id": result.get("id"),
        "contextId": result.get("contextId"),
        "state": status.get("state") if isinstance(status, dict) else None,
        "trace_id": metadata.get("trace_id") if isinstance(metadata, dict) else None,
        "artifact_count": len(result.get("artifacts") or []),
    }


def _agent_card_summary(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    return {
        "ok": response["ok"],
        "status_code": response["status_code"],
        "protocolVersion": body.get("protocolVersion"),
        "name": body.get("name"),
        "url": body.get("url"),
        "supportedMethods": body.get("supportedMethods", []),
        "authentication": _safe_value(body.get("authentication", {})),
        "skill_ids": [skill.get("id") for skill in body.get("skills", []) if isinstance(skill, dict)],
    }


def _compact_response_body(url: str, body: Any) -> Any:
    if not isinstance(body, dict):
        return body
    if url.startswith("/control-plane/traces/"):
        trace = body.get("trace", {}) if isinstance(body.get("trace"), dict) else {}
        gateway = body.get("gateway", {}) if isinstance(body.get("gateway"), dict) else {}
        return {
            "schema_version": body.get("schema_version"),
            "run_id": body.get("run_id"),
            "trace_id": body.get("trace_id"),
            "gateway": _compact_gateway_record(gateway),
            "trace": _compact_trace_summary(trace),
            "redaction": body.get("redaction", {}),
        }
    if url.endswith("/audit") or url.startswith("/control-plane/audit/events"):
        return _compact_audit_events(body)
    if "/control-plane/runs/" in url:
        if url.endswith("/route"):
            return {
                "schema_version": body.get("schema_version"),
                "run_id": body.get("run_id"),
                "trace_id": body.get("trace_id"),
                "route_status": body.get("route_status"),
                "route_decision": body.get("route_decision", {}),
                "failure_class": body.get("failure_class"),
                "redaction": body.get("redaction", {}),
            }
        if url.endswith("/delegation-tree"):
            return {
                "schema_version": body.get("schema_version"),
                "parent_run_id": body.get("parent_run_id"),
                "parent_trace_id": body.get("parent_trace_id"),
                "root": body.get("root", {}),
                "child_count": len(body.get("children") or []),
                "children": body.get("children", []),
                "redaction": body.get("redaction", {}),
            }
        if url.endswith("/budget"):
            return {
                "schema_version": body.get("schema_version"),
                "run_id": body.get("run_id"),
                "trace_id": body.get("trace_id"),
                "budget": body.get("budget", {}),
                "cost": body.get("cost", {}),
                "latency_ms": body.get("latency_ms"),
                "redaction": body.get("redaction", {}),
            }
        if url.endswith("/replay-preview"):
            request = body.get("request", {}) if isinstance(body.get("request"), dict) else {}
            return {
                "schema_version": body.get("schema_version"),
                "run_id": body.get("run_id"),
                "trace_id": body.get("trace_id"),
                "request": {
                    "entrypoint": request.get("entrypoint"),
                    "user_id": request.get("user_id"),
                    "session_id": request.get("session_id"),
                    "message": request.get("message"),
                },
                "route_decision": body.get("route_decision", {}),
                "delegated_task_count": len(body.get("delegated_tasks") or []),
                "failure_class": body.get("failure_class"),
                "replay_notes": body.get("replay_notes", []),
                "redaction": body.get("redaction", {}),
            }
        return _compact_run_summary(body)
    if url == "/control-plane/readiness":
        return body
    return body


def _compact_run_summary(body: dict[str, Any]) -> dict[str, Any]:
    trace = body.get("trace", {}) if isinstance(body.get("trace"), dict) else {}
    return {
        "schema_version": body.get("schema_version"),
        "run_id": body.get("run_id"),
        "trace_id": body.get("trace_id"),
        "status": body.get("status"),
        "source": body.get("source"),
        "route_decision": body.get("route_decision", {}),
        "delegated_task_count": len(body.get("delegated_tasks") or []),
        "identity": body.get("identity", {}),
        "budget": body.get("budget", {}),
        "cost": body.get("cost", {}),
        "latency_ms": body.get("latency_ms"),
        "failure_class": body.get("failure_class"),
        "error_count": body.get("error_count"),
        "trace": _compact_trace_summary(trace),
        "redaction": body.get("redaction", {}),
    }


def _compact_gateway_record(gateway: dict[str, Any]) -> dict[str, Any]:
    if not gateway:
        return {}
    return {
        "run_id": gateway.get("run_id"),
        "trace_id": gateway.get("trace_id"),
        "status": gateway.get("status"),
        "entrypoint": gateway.get("entrypoint"),
        "route_decision": gateway.get("route_decision", {}),
        "delegated_task_count": len(gateway.get("delegated_tasks") or []),
        "identity": gateway.get("identity", {}),
        "budget": gateway.get("budget", {}),
        "cost": gateway.get("cost", {}),
        "latency_ms": gateway.get("latency_ms"),
        "error_count": gateway.get("error_count"),
        "redaction": gateway.get("redaction", {}),
    }


def _compact_trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    if not trace:
        return {}
    return {
        "run_id": trace.get("run_id"),
        "trace_id": trace.get("trace_id"),
        "event_count": trace.get("event_count") or len(trace.get("events") or []),
        "error_count": trace.get("error_count"),
        "retry_count": trace.get("retry_count"),
        "node_path": trace.get("node_path", []),
        "tools": trace.get("tools", []),
        "providers": trace.get("providers", []),
        "budget_exceeded": trace.get("budget_exceeded"),
    }


def _compact_audit_events(body: dict[str, Any]) -> dict[str, Any]:
    events = body.get("events", []) if isinstance(body.get("events"), list) else []
    return {
        "schema_version": body.get("schema_version"),
        "total": body.get("total", len(events)),
        "event_types": sorted({event.get("event_type") for event in events if isinstance(event, dict) and event.get("event_type")}),
        "events": [
            {
                "event_type": event.get("event_type"),
                "component": event.get("component"),
                "action": event.get("action"),
                "outcome": event.get("outcome"),
                "run_id": event.get("run_id"),
                "trace_id": event.get("trace_id"),
            }
            for event in events
            if isinstance(event, dict)
        ],
        "retention": body.get("retention", {}),
        "redaction": body.get("redaction", {}),
    }


def _operator_context(*, user_id: str, session_id: str) -> dict[str, Any]:
    return _safe_value(
        {
            "mode": "offline_same_process",
            "dotenv_loading": "disabled_by_default",
            "user_id": user_id,
            "session_id": session_id,
            "auth_mode": os.environ.get("MULTIMODAL_AGENT_AUTH_MODE"),
            "auth_bound_identity_required": os.environ.get("MULTIMODAL_AGENT_REQUIRE_AUTH_BOUND_IDENTITY"),
            "runtime_profile": os.environ.get("MULTIMODAL_AGENT_RUNTIME_PROFILE"),
            "chat_provider": os.environ.get("MULTIMODAL_AGENT_CHAT_PROVIDER"),
            "image_provider": os.environ.get("MULTIMODAL_AGENT_IMAGE_PROVIDER"),
            "remote_calls": "not_performed",
            "real_provider_calls": "not_performed",
        }
    )


def _redaction_checks(payload: dict[str, Any]) -> dict[str, Any]:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    present = [marker for marker in SENSITIVE_TEXT_MARKERS if marker.lower() in rendered.lower()]
    return {
        "raw_auth_tokens_included": False,
        "provider_raw_responses_included": False,
        "inline_media_bodies_included": False,
        "sensitive_text_markers_present": present,
        "passed": not present,
    }


def _overall_status(payload: dict[str, Any]) -> str:
    required_paths = [
        ("readiness",),
        ("agent_card",),
        ("single_agent_run", "response"),
        ("single_agent_run", "control_plane", "run"),
        ("single_agent_run", "control_plane", "trace"),
        ("gateway_run", "response"),
        ("gateway_run", "control_plane", "route"),
        ("gateway_run", "control_plane", "delegation_tree"),
        ("gateway_run", "control_plane", "budget"),
        ("gateway_run", "control_plane", "audit"),
        ("gateway_run", "control_plane", "replay_preview"),
        ("a2a_inbound_run", "response"),
        ("a2a_inbound_run", "control_plane", "route"),
        ("control_plane_recent_audit",),
    ]
    missing_or_failed = [path for path in required_paths if not _path_ok(payload, path)]
    if missing_or_failed or not payload["redaction_checks"]["passed"]:
        payload["failed_gates"] = [".".join(path) for path in missing_or_failed]
        return "failed"
    return "passed"


def _path_ok(payload: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    if isinstance(current, dict) and "ok" in current:
        return bool(current["ok"])
    return bool(current)


def _body_value(response: dict[str, Any], key: str) -> str | None:
    body = response.get("body")
    if isinstance(body, dict):
        value = body.get(key)
        return str(value) if value else None
    return None


def _safe_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                safe[key_text] = SENSITIVE_KEY_REPLACEMENT
            else:
                safe[key_text] = _safe_value(item)
        return safe
    if isinstance(value, str):
        return _safe_text(value)
    return value


def _safe_text(value: str) -> str:
    cleaned = value
    for marker in SENSITIVE_TEXT_MARKERS:
        if marker in cleaned:
            cleaned = cleaned.replace(marker, SENSITIVE_KEY_REPLACEMENT)
    return cleaned


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered.endswith("_included"):
        return False
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _apply_offline_env_defaults() -> None:
    for key, value in OFFLINE_ENV_DEFAULTS.items():
        os.environ[key] = value


def _output_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


if __name__ == "__main__":
    raise SystemExit(main())
