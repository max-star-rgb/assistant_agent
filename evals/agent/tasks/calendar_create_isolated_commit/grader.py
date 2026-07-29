"""Grader for one isolated calendar commit."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "calendar_commit_confirmation"
RUBRIC = """
通过条件：
1. 回答明确确认已创建“项目复盘”，时间为明天下午两点到三点，地点会议室B。
2. 回答不得声称创建了额外事件或未提交的字段。
判定边界：只判断确认内容是否忠于 calendar_create Evidence。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    args = evidence.tool_executions[0].input if evidence.tool_executions else {}
    passed = (
        "项目复盘" in str(args.get("title") or "")
        and "14:00" in str(args.get("start_time") or "")
        and "15:00" in str(args.get("end_time") or "")
        and args.get("location") == "会议室B"
    )
    return passed, f"input={args}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("calendar_create",),
        expected_sequence=["calendar_create"],
        argument_check=_arguments,
        state_changes=True,
    )
