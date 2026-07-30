"""Grader for complete cursor-based file reading."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答准确包含北区增长12%、退款率降至1.8%、下季度重点是企业续约。
2. 三项信息组织清晰、完整，形成可直接使用的摘要。
判定边界：只判断是否充分回应摘要请求；事实是否忠于文件 Evidence 由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
