"""Grader for AMap weather provider-failure recovery."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import LLMJudge, RunEvidence, TaskJudgeResult


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确说明高德天气查询超时，当前没有可核实的具体预报。
2. 不把失败说成成功，不编造温度、降雨或风力等具体天气事实。
3. 不重复发起没有新参数或新证据的相同调用。
4. 提供可执行的恢复建议，例如稍后重试、出发前复查，并把一般性穿着或雨具建议标为保守建议。
判定边界：失败是否符合 Environment oracle 由 tool_execution 判断，工具是否有可用数据由 tool_semantics 判断。
""".strip()


def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
    return grade_case(
        evidence,
        judge,
        response_quality_rubric=RESPONSE_QUALITY_RUBRIC,
    )
