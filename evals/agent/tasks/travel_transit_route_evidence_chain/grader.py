"""Grader for named-place transit evidence chaining."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 说明杭州东站和中国丝绸博物馆的坐标已经先被解析，并以这些坐标规划公交路线。
2. 给出公共交通方案的主要线路或摘要。
3. 给出预计时间、步行距离和换乘次数。
判定边界：只判断是否完整回应路线请求和交代证据链；具体路线字段是否忠于工具 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
