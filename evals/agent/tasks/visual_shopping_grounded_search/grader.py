"""Grader for visual observation translated into shopping input."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "visual_shopping_grounding"
RUBRIC = """
通过条件：
1. 回答和商品搜索以图片 Evidence 的白色、低帮、皮革运动鞋特征为依据。
2. 推荐价格不得超过用户400元预算，且不得声称已购买。
判定边界：不要求固定品牌或固定商品。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    calls = evidence.tool_executions
    shopping = calls[1].input if len(calls) == 2 else {}
    query = str(shopping.get("query") or "")
    passed = (
        len(calls) == 2
        and "白色" in query
        and ("运动鞋" in query or "鞋" in query)
        and shopping.get("budget_max") == 400
    )
    return passed, f"inputs={[item.input for item in calls]}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("media_inspect", "shopping_search"),
        expected_sequence=["media_inspect", "shopping_search"],
        argument_check=_arguments,
    )
