"""Grader for contact-resolved calendar writing."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答确认创建“需求确认”日程，并说明已邀请 Alex Chen。
2. 创建与邀请结果表达清晰、完整，用户无需追问处理状态。
判定边界：只判断是否完整回应日程创建请求；身份和提交事实是否可信由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
