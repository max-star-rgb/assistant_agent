"""Grader for proactive travel Skill loading."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 给出苏州园景酒店作为符合每晚600元以内的可订候选。
2. 区分每晚568元与两晚估算总价1136元。
3. 提供工具返回的 OTA 跳转链接。
4. 说明跳转不代表锁价或预订成功，库存、税费和退改以 OTA 页面为准。
判定边界：只判断是否完整回应住宿候选、价格口径和链接交付；是否调用内部 Skill 由 tool_execution Rule 判断，具体字段是否忠于工具 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
