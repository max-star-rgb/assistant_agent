"""Grader for structured cross-tool evidence use."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "weather_shopping_grounding"
RUBRIC = """
通过条件：
1. 回答中的天气判断忠于 weather Evidence。
2. 商品建议和是否满足500元总预算忠于 shopping_list_search Evidence。
3. 不得声称已下单。
判定边界：不要求固定商品或固定措辞。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    calls = evidence.tool_executions
    shopping = calls[1].input if len(calls) >= 2 else {}
    evidence_items = shopping.get("evidence", [])
    needs = shopping.get("needs", [])
    passed = (
        len(calls) >= 2
        and calls[0].name == "weather"
        and calls[1].name == "shopping_list_search"
        and all(item.name == "shopping_search" for item in calls[2:])
        and "上海" in str(calls[0].input.get("location") or "")
        and shopping.get("total_budget") == 500
        and len(needs) == 2
        and bool(evidence_items)
        and evidence_items[0].get("source_tool") == "weather"
    )
    return passed, f"inputs={[item.input for item in calls]}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    sequence = [
        item.name or ""
        for item in evidence.tool_executions
    ]
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("weather", "shopping_list_search"),
        expected_sequence=sequence,
        argument_check=_arguments,
    )
