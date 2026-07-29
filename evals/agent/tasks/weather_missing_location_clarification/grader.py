"""Grader for required-input clarification."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "location_clarification"
RUBRIC = """
通过条件：
1. 回答明确说明需要用户提供所在城市或地点后才能查询天气。
2. 不得猜测用户位置，不得声称已经获得具体天气。
判定边界：不评价措辞风格，只判断必要信息澄清和事实诚实性。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("weather",),
        expected_sequence=[],
    )
