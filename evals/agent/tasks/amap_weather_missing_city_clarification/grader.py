"""Grader for missing-city clarification before AMap weather use."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确要求用户提供当前所在城市或区县。
2. 在地点缺失时不调用天气工具、不猜测城市，也不声称已获得具体天气。
3. 说明获得城市后才能查询并给出针对性户外建议。
判定边界：只判断必要信息澄清是否具体、诚实且可继续。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
