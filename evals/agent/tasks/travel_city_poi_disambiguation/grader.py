"""Grader for city-scoped POI disambiguation."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确目标地点是中国丝绸博物馆，而不是丝博文创商店。
2. 给出目标博物馆的地址和坐标。
3. 解释用于消歧的地点类型或名称证据。
判定边界：只判断是否完整回应 POI 确认与消歧请求；具体字段是否忠于工具 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
