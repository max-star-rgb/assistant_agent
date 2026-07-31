"""Grader for complete cursor-based file reading."""

from evals.agent.batch_grading import grader_for_response_quality

RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 回答准确包含北区增长12%、退款率降至1.8%、下季度重点是企业续约。
2. 三项信息组织清晰、完整，形成可直接使用的摘要。
判定边界：只判断是否充分回应摘要请求；事实是否忠于文件 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
