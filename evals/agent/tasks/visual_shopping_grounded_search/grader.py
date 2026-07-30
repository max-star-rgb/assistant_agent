"""Grader for visual observation translated into shopping input."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答先说明识别到的鞋款特征，再推荐相似款。
2. 推荐明确说明价格且不超过用户 400 元预算，表达清晰、便于选择。
判定边界：不要求固定品牌或商品；款式和价格是否忠于 Evidence 由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
