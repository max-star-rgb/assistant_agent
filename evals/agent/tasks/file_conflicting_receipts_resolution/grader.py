"""Grader for resolving duplicate and conflicting receipt evidence."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 明确说明当前有发票凭证支持的金额为860元。
2. 识别两份发票的发票号码相同，属于重复材料，不能重复计入。
3. 指出支付总额920元与发票金额相差60元。
4. 说明现有材料只把差额标为服务费，仍需服务费发票或费用明细。
5. 清晰区分已证实事实与待补证信息，不猜测最终报销资格。
判定边界：只判断是否完整回应核对请求；事实是否忠于文件 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
