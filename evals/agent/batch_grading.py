"""Shared aggregation for batch Task-local graders."""

from __future__ import annotations

from dataclasses import dataclass

from evals.agent.contracts import (
    LLMJudge,
    RunEvidence,
    TaskJudgeResult,
)
from evals.agent.grading import (
    dimension,
    judge_assertion,
    task_judge_result,
)

TOOL_SEMANTICS_CRITERION_ID = "tool_semantics"
TOOL_SEMANTICS_RUBRIC = """
判断工具返回内容本身是否语义正确且可用，采用阳性判断。
通过条件：
1. 已调用工具的返回内容与该工具请求一致，结构和字段含义自洽，可被 Agent 正确理解和使用。
2. 合法的空结果或“未找到”属于可用业务结果，可以通过。
3. 没有调用工具时，视为不存在错误的工具返回，可以通过。
失败条件：
1. 工具返回 provider/runtime 错误，例如超时、鉴权失败或服务不可用。
2. 返回内容损坏、互相矛盾、答非所问，或缺少完成其业务语义所需的关键数据。
判定边界：不判断 Environment 是否原本就预期该成功或失败，不判断 Agent 最终回答。
""".strip()

GROUNDING_CRITERION_ID = "grounding"
GROUNDING_RUBRIC = """
判断 Agent 最终回答是否忠于实际工具结果，采用阳性判断。
通过条件：
1. 回答对工具成功、失败、空结果和具体字段的理解与 Evidence 一致。
2. 回答中的工具归因事实可以在对应工具结果中找到；没有工具结果时，不虚构工具已提供事实。
3. 可以省略部分工具字段，也可以明确区分来自其他能力或一般建议的内容。
失败条件：
1. 把工具失败说成成功、把空结果说成找到了数据，或编造工具未返回的具体事实。
2. 给出与工具结果明显冲突的结论，却没有说明不确定性或其他证据来源。
判定边界：只判断对工具证据的忠实性，不判断回答是否完整、清晰或真正解决用户问题。
""".strip()

RESPONSE_QUALITY_CRITERION_ID = "response_quality"


@dataclass(frozen=True)
class ResponseQualityGrader:
    """Callable Task grader binding one local response-quality rubric."""

    response_quality_rubric: str

    def __call__(
        self,
        evidence: RunEvidence,
        judge: LLMJudge,
    ) -> TaskJudgeResult:
        return grade_case(
            evidence,
            judge,
            response_quality_rubric=self.response_quality_rubric,
        )


def grader_for_response_quality(rubric: str) -> ResponseQualityGrader:
    """Create the standard three-dimension grader for one Task rubric."""

    normalized = rubric.strip()
    if not normalized:
        raise ValueError("Response-quality rubric must not be empty.")
    return ResponseQualityGrader(response_quality_rubric=normalized)


def grade_case(
    evidence: RunEvidence,
    judge: LLMJudge,
    *,
    response_quality_rubric: str,
) -> TaskJudgeResult:
    tool_semantics = judge.evaluate(
        criterion_id=TOOL_SEMANTICS_CRITERION_ID,
        rubric=TOOL_SEMANTICS_RUBRIC,
        evidence=evidence,
    )
    grounding = judge.evaluate(
        criterion_id=GROUNDING_CRITERION_ID,
        rubric=GROUNDING_RUBRIC,
        evidence=evidence,
    )
    response_quality = judge.evaluate(
        criterion_id=RESPONSE_QUALITY_CRITERION_ID,
        rubric=(
            "判断最终回答是否真正回应当前用户请求，并且表达清晰、完整、有用。"
            "采用阳性判断；不要仅因工具失败或空结果而判失败，只判断 Agent 是否在现有"
            "条件下给出了恰当回答。\n\n本 Task 的具体通过条件：\n"
            + response_quality_rubric
        ),
        evidence=evidence,
    )
    return task_judge_result(
        tool_semantics=dimension(
            {
                TOOL_SEMANTICS_CRITERION_ID: judge_assertion(
                    tool_semantics,
                    criterion_id=TOOL_SEMANTICS_CRITERION_ID,
                    label="工具返回语义正确且可用",
                ),
            }
        ),
        grounding=dimension(
            {
                GROUNDING_CRITERION_ID: judge_assertion(
                    grounding,
                    criterion_id=GROUNDING_CRITERION_ID,
                    label="回答忠于工具结果",
                ),
            }
        ),
        response_quality=dimension(
            {
                RESPONSE_QUALITY_CRITERION_ID: judge_assertion(
                    response_quality,
                    criterion_id=RESPONSE_QUALITY_CRITERION_ID,
                    label="回答清晰完整地回应用户",
                ),
            }
        ),
    )
