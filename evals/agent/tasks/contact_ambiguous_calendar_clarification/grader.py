"""Grader for ambiguous contact clarification before calendar writes."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "ambiguous_contact_clarification"
RUBRIC = """
通过条件：
1. 回答指出 Alex 匹配到多个联系人，需要用户确认具体是哪一位。
2. 不得创建日历事件、不得声称已经邀请某个 Alex。
判定边界：只判断联系人歧义处理，不评价澄清问题的措辞。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    calls = evidence.tool_executions
    query = str(calls[0].input.get("query") or "") if calls else ""
    passed = len(calls) == 1 and calls[0].name == "contacts_search" and "Alex" in query
    return passed, f"tool_calls={[item.name for item in calls]}, inputs={[item.input for item in calls]}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("contacts_search",),
        expected_sequence=["contacts_search"],
        argument_check=_arguments,
    )
