"""Response-quality grader for empty visual-history results."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确说明当前会话已保留的历史画面中没有找到蓝色 U 盘的相关记录。
2. 不编造 U 盘曾出现、仍在某处或具有具体位置。
3. 区分“历史记录未找到”与“确定物体不存在”；可以给出有限的继续查找建议。
4. 回答直接、自然，不把空结果包装成已确认线索。
判定边界：工具成功返回合法空结果由 tool_execution 与 tool_semantics 分别判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
