"""Grader for contact-resolved calendar writing."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "contact_calendar_confirmation"
RUBRIC = """
通过条件：
1. 回答确认创建“需求确认”日程，并说明已邀请 Alex Chen。
2. 邀请信息必须来自 contacts_search Evidence，不得编造其他联系人。
判定边界：只判断跨工具身份解析和提交确认。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    calls = evidence.tool_executions
    create = calls[1].input if len(calls) == 2 else {}
    passed = (
        len(calls) == 2
        and "Alex Chen" in str(calls[0].input.get("query") or "")
        and "需求确认" in str(create.get("title") or "")
        and create.get("attendees") == ["alex.chen@example.test"]
        and "16:00" in str(create.get("start_time") or "")
    )
    return passed, f"inputs={[item.input for item in calls]}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("contacts_search", "calendar_create"),
        expected_sequence=["contacts_search", "calendar_create"],
        argument_check=_arguments,
        state_changes=True,
    )
