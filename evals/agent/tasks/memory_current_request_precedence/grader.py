"""Grader for current-request authority over remembered preference."""

from evals.agent.batch_grading import grader_for_response_quality

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答只推荐无糖饮料，并给出三种选择。
2. 三种选择表达清晰、可执行，直接回应本轮无糖要求。
判定边界：不评价具体品牌或营养建议；回答是否受无关历史信息影响由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
