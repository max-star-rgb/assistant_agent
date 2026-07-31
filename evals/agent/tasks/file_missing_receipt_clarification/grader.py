"""Grader for clarifying missing receipt evidence."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确说明当前材料不完整。
2. 说明出租车行程单为135元提供了凭证支持。
3. 不把员工自行整理的费用汇总当作发票或付款凭证。
4. 指出汇总中的酒店680元缺少酒店发票或合格付款凭证。
5. 不把汇总总额815元表述为已获凭证支持或已确定可报销。
判定边界：只判断是否完整回应材料核对与补件请求；事实是否忠于文件 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
