"""Grader for relative-date grounding in AMap forecasts."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 使用高德返回中日期等于“明天”的 forecast，而不是今天或后天。
2. 针对下午活动使用白天天气和白天气温，必要时可以补充夜间数据但不得混淆。
3. 明确判断是否适合步行，并给出穿着和雨具建议。
4. 不把日级预报表述为下午三点的精确小时预报。
判定边界：具体天气字段是否忠于工具 Evidence 由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
