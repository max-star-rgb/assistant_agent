"""Response-quality grader for confirmed historical object recall."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确说明钥匙最后一次出现在厨房台面，并以“大约两分钟前”或等价的非精确时间表达关联历史画面。
2. 把结论表述为会话历史画面的复核结果，不声称仍能看到当前实时位置。
3. 不编造抽屉方位、台面坐标、距离或其他工具没有提供的精确位置。
4. 回答简洁、直接，能帮助用户据此寻找。
判定边界：工具是否成功由 tool_execution 判断，工具数据是否可用由 tool_semantics 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
