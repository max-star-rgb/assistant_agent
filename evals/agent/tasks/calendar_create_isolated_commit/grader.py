"""Grader for one isolated calendar commit."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答明确确认已创建“项目复盘”，时间为明天下午两点到三点，地点会议室B。
2. 确认信息表达清晰、完整，用户无需追问创建结果。
判定边界：只判断是否完整回应创建日程的请求；事实是否忠于工具结果由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
