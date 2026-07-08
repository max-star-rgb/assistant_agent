"""Run offline E2E demo scenarios for the assistant agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from assistant_agent.config import ProviderConfig
from assistant_agent.gateway import GatewaySessionManager
from assistant_agent.realtime import GatewayAgentAdapter
from assistant_agent.schemas.api import api_error_from_agent_error
from assistant_agent.services.assistant_run_service import create_runtime
from assistant_agent.services.assistant_runtime_app import AssistantRuntimeApp
from assistant_agent.services.gateway_turn_facade import GatewayTurnFacade, GatewayTurnRequest


SCENARIO_PATH = REPO_ROOT / "demo_data" / "scenarios" / "e2e_demo_scenarios.json"
GENERIC_RESPONSE_TEXT = "已完成请求处理。"
SENSITIVE_KEYS = {"api_key", "authorization", "bearer", "token", "secret", "base64", "provider_response", "raw"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run offline E2E demo flows. Defaults always use mock/local providers.",
    )
    parser.add_argument(
        "--scenario",
        help="Run one scenario_id from demo_data/scenarios/e2e_demo_scenarios.json.",
    )
    parser.add_argument(
        "--scenarios-path",
        default=str(SCENARIO_PATH),
        help="Path to the demo scenario matrix JSON file.",
    )
    return parser


def load_scenarios(path: Path = SCENARIO_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def select_scenarios(
    scenarios: list[dict[str, Any]],
    scenario_id: str | None,
) -> list[dict[str, Any]]:
    if scenario_id is None:
        return scenarios
    selected = [scenario for scenario in scenarios if scenario["scenario_id"] == scenario_id]
    if not selected:
        raise ValueError(f"Unknown scenario_id: {scenario_id}")
    return selected


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_run_scenario_through_gateway(scenario))


async def _run_scenario_through_gateway(scenario: dict[str, Any]) -> dict[str, Any]:
    app = AssistantRuntimeApp(
        runtime_factory=lambda: create_runtime(config=ProviderConfig(), load_env=False)
    )
    captured: list[Any] = []

    def run_request(request, **kwargs: Any) -> Any:
        artifacts = app.run_request(request, **kwargs)
        captured.append(artifacts)
        return artifacts

    manager = GatewaySessionManager(
        backend_factory=lambda: GatewayAgentAdapter(
            run_request=run_request,
            load_env=False,
        ),
        start_reaper=False,
    )
    facade = GatewayTurnFacade(manager=manager)
    try:
        await facade.run_turn(_gateway_request_from_scenario(scenario))
    finally:
        await manager.close()

    if not captured:
        raise RuntimeError(
            f"Gateway demo scenario {scenario['scenario_id']} completed without assistant artifacts."
        )
    return _result_from_state(scenario, captured[-1].state)


def _result_from_state(scenario: dict[str, Any], state: Any) -> dict[str, Any]:
    response_text = state.response.message if state.response else ""
    tool_sequence = [call.tool_name for call in state.tool_calls]
    errors = [_api_error_payload(error) for error in state.errors]
    expected_tools = scenario.get("expected_tools", [])
    expected_response_contains = scenario.get("expected_response_contains", [])
    checks = {
        "expected_tools_match": _tools_contain_expected(tool_sequence, expected_tools),
        "response_contains_match": all(
            expected in response_text for expected in expected_response_contains
        ),
        "non_generic_response": bool(response_text and response_text != GENERIC_RESPONSE_TEXT),
    }
    status = "succeeded" if state.status != "failed" else "failed"
    return _sanitize_payload(
        {
            "scenario_id": scenario["scenario_id"],
            "status": status,
            "tool_sequence": tool_sequence,
            "response_text": response_text,
            "errors": errors,
            "run_id": state.run_id,
            "trace_id": state.trace_id,
            "checks": checks,
        }
    )


def run_demo_flows(
    scenario_id: str | None = None,
    scenarios_path: Path = SCENARIO_PATH,
) -> dict[str, Any]:
    scenarios = select_scenarios(load_scenarios(scenarios_path), scenario_id)
    results = [run_scenario(scenario) for scenario in scenarios]
    failed_ids = [result["scenario_id"] for result in results if result["status"] != "succeeded"]
    return {
        "total": len(results),
        "passed": len(results) - len(failed_ids),
        "failed": len(failed_ids),
        "failed_scenario_ids": failed_ids,
        "results": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run_demo_flows(
            scenario_id=args.scenario,
            scenarios_path=Path(args.scenarios_path),
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


def _gateway_request_from_scenario(scenario: dict[str, Any]) -> GatewayTurnRequest:
    metadata, image_ids, video_ids, audio_id = _scenario_metadata_and_media(scenario)
    metadata["offline"] = True
    gateway_metadata = metadata.get("gateway")
    if not isinstance(gateway_metadata, dict):
        gateway_metadata = {}
    metadata["gateway"] = {
        **gateway_metadata,
        "suppress_realtime_backend_source": True,
    }
    return GatewayTurnRequest(
        user_id="demo_user",
        session_id=f"demo_{scenario['scenario_id']}",
        text=str(scenario.get("user_query") or ""),
        image_ids=image_ids,
        video_ids=video_ids,
        audio_id=audio_id,
        metadata=metadata,
    )


def _scenario_metadata_and_media(
    scenario: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str], str | None]:
    metadata = dict(scenario.get("metadata", {}))
    image_ids = list(metadata.pop("image_ids", scenario.get("image_ids", [])))
    video_ids = list(metadata.pop("video_ids", scenario.get("video_ids", [])))
    audio_id = metadata.pop("audio_id", scenario.get("audio_id", None))
    return metadata, image_ids, video_ids, str(audio_id) if audio_id is not None else None


def _tools_contain_expected(actual_tools: list[str], expected_tools: list[str]) -> bool:
    cursor = 0
    for tool_name in actual_tools:
        if cursor < len(expected_tools) and tool_name == expected_tools[cursor]:
            cursor += 1
    return cursor == len(expected_tools)


def _api_error_payload(error: Any) -> dict[str, Any]:
    return api_error_from_agent_error(error).model_dump(mode="json")


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SENSITIVE_KEYS):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        lowered = value.lower()
        if "data:image/" in lowered and "base64," in lowered:
            return "[redacted_base64]"
        if "bearer " in lowered or "authorization" in lowered:
            return "[redacted]"
    return value


if __name__ == "__main__":
    raise SystemExit(main())
