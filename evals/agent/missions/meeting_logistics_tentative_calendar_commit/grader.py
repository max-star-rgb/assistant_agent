"""Grader for the tentative meeting-logistics calendar Mission."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确确认已创建一条“暂定”日历事件，而不是只给出计划或建议。
2. 清晰汇总 2026 年 9 月 18 日 14:00–17:00、上海青浦万达茂、上海虹桥站公共交通方案。
3. 清晰说明 9 月 17 日至 19 日、4 间房、最近的当前可用酒店及每晚每间价格。
4. 明确没有发送邀请、预订酒店或付款，并保留库存和价格仍需复核的边界。
判定边界：只判断回答是否完整回应请求；工具结果是否真实、日历是否实际落盘分别由 grounding、
tool_execution 和 Mission objective state Rule 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
