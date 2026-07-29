"""Grader for current-request authority over remembered preference."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "current_request_precedence"
RUBRIC = """
通过条件：
1. 回答只推荐无糖饮料，并给出三种选择。
2. 不得因为历史记忆中的含糖可乐偏好而推荐含糖饮料。
判定边界：不评价具体品牌或营养建议，只判断上下文权威顺序。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=(),
        expected_sequence=None,
    )
