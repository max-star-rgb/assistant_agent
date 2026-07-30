"""Grader for lodging constraint and price-basis grounding."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 最多给出三个符合每晚不超过600元且靠近中国丝绸博物馆的候选。
2. 对每个候选区分每晚价和三晚总价。
3. 说明总价由展示每晚价乘晚数估算，不把它表述为含税或已锁定的最终成交价。
4. 提醒价格、库存和退改条件以 OTA 页面为准。
判定边界：只判断是否完整回应住宿筛选和价格解释请求；具体候选与价格是否忠于工具 Evidence 由 grounding 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
