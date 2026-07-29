"""Grader for search-to-fetch grounded research."""

from evals.agent.batch_grading import grade_case
from evals.agent.contracts import GraderResult, LLMJudge, RunEvidence

CRITERION_ID = "fetched_source_grounding"
RUBRIC = """
通过条件：
1. 回答给出的发布日期必须是 2026-08-18。
2. 回答指出新增功能为审计日志和批量导出。
3. 不得添加 fetch Evidence 中不存在的发布事实。
判定边界：只判断正文证据忠实性，不要求固定措辞。
""".strip()


def _arguments(evidence: RunEvidence) -> tuple[bool, str]:
    calls = evidence.tool_executions
    passed = (
        len(calls) == 2
        and bool(str(calls[0].input.get("query") or "").strip())
        and calls[1].input.get("url") == "https://example.test/zephyr-release"
    )
    return passed, f"inputs={[item.input for item in calls]}"


def grade(evidence: RunEvidence, judge: LLMJudge) -> GraderResult:
    return grade_case(
        evidence,
        judge,
        criterion_id=CRITERION_ID,
        rubric=RUBRIC,
        expected_tools=("web_search", "web_fetch"),
        expected_sequence=["web_search", "web_fetch"],
        argument_check=_arguments,
    )
