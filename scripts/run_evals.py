"""Run offline Agent eval cases."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from multimodal_agent.agent.workflow import AgentWorkflow
from multimodal_agent.schemas.capabilities import canonical_intent
from multimodal_agent.schemas.requests import UserRequest


DEFAULT_CASES_PATH = ROOT / "tests" / "evals" / "eval_cases.json"


def load_cases(path: Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def tools_contain_expected(actual_tools: list[str], expected_tools: list[str]) -> bool:
    cursor = 0
    for tool_name in actual_tools:
        if cursor < len(expected_tools) and tool_name == expected_tools[cursor]:
            cursor += 1
    return cursor == len(expected_tools)


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    request = UserRequest(
        user_id=case.get("user_id", "u1"),
        session_id=case.get("session_id", "s1"),
        text=case.get("text"),
        image_ids=case.get("image_ids", []),
        video_ids=case.get("video_ids", []),
        audio_id=case.get("audio_id"),
    )
    state = AgentWorkflow().run(request)
    actual_intent = state.intent.intent if state.intent else None
    actual_tools = [call.tool_name for call in state.tool_calls]
    expected_tools = case.get("expected_tools", [])
    expected_intent = case.get("expected_intent")
    must_not_call = case.get("must_not_call", [])

    intent_match = (
        actual_intent == expected_intent
        if actual_intent is None or expected_intent is None
        else canonical_intent(actual_intent) == canonical_intent(expected_intent)
    )
    tool_selection_match = set(expected_tools).issubset(set(actual_tools))
    ordered_tool_match = tools_contain_expected(actual_tools, expected_tools)
    unexpected_tools = [tool for tool in actual_tools if tool in must_not_call]
    passed = intent_match and tool_selection_match and ordered_tool_match and not unexpected_tools
    return {
        "id": case["id"],
        "category": case.get("category"),
        "passed": passed,
        "intent_match": intent_match,
        "tool_selection_match": tool_selection_match,
        "ordered_tool_match": ordered_tool_match,
        "unexpected_tool_called": bool(unexpected_tools),
        "expected_intent": expected_intent,
        "actual_intent": actual_intent,
        "expected_tools": expected_tools,
        "actual_tools": actual_tools,
        "must_not_call": must_not_call,
        "unexpected_tools": unexpected_tools,
    }


def run_evals(cases: list[dict[str, Any]]) -> dict[str, Any]:
    details = [evaluate_case(case) for case in cases]
    failed_case_ids = [detail["id"] for detail in details if not detail["passed"]]

    total = len(cases)
    failed = len(failed_case_ids)
    passed_count = total - failed
    intent_matches = sum(1 for detail in details if detail["intent_match"])
    tool_selection_matches = sum(1 for detail in details if detail["tool_selection_match"])
    ordered_tool_matches = sum(1 for detail in details if detail["ordered_tool_match"])
    unexpected_tool_cases = sum(1 for detail in details if detail["unexpected_tool_called"])
    return {
        "total": total,
        "passed": passed_count,
        "failed": failed,
        "pass_rate": passed_count / total if total else 0.0,
        "intent_accuracy": intent_matches / total if total else 0.0,
        "tool_selection_accuracy": tool_selection_matches / total if total else 0.0,
        "ordered_tool_match": ordered_tool_matches / total if total else 0.0,
        "unexpected_tool_rate": unexpected_tool_cases / total if total else 0.0,
        "failed_case_ids": failed_case_ids,
    }


def main() -> int:
    summary = run_evals(load_cases())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
