"""Grader for complete cursor-based file reading."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "complete_file_grounding"
RUBRIC = """
通过条件：
1. 回答准确包含北区增长12%、退款率降至1.8%、下季度重点是企业续约。
2. 不得添加文件 Evidence 中不存在的具体经营数字。
判定边界：只判断完整读取后的事实忠实性，不评价摘要风格。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    calls = evidence.tool_executions
    cursors = [item.input.get("cursor", 0) for item in calls]
    passed = (
        len(calls) >= 2
        and all(item.input.get("path") == "quarterly-brief.md" for item in calls)
        and cursors[0] == 0
        and all(
            isinstance(cursor, int) and cursor > 0 for cursor in cursors[1:]
        )
        and cursors == sorted(set(cursors))
    )
    return passed, f"cursors={cursors}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    sequence = ["file_read"] * len(evidence.tool_executions)
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("file_read",),
        expected_sequence=sequence,
        argument_check=_arguments,
    )
