"""Grader for lodging constraint and price-basis grounding."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 最多给出三个符合每晚不超过600元且靠近中国丝绸博物馆的候选。
2. 对每个候选区分每晚价和三晚总价。
3. 说明总价由展示每晚价乘晚数估算，不把它表述为含税或已锁定的最终成交价。
4. 提醒价格、库存和退改条件以 OTA 页面为准。
5. lodging_search 为候选提供 booking_url 时，回答给出对应的可点击 OTA 跳转链接，并且不把链接表述为已经预订、保证可用或保证原价；没有 booking_url 时不得建议点击不存在的链接。
判定边界：只判断是否完整回应住宿筛选、价格解释和链接交付请求；具体候选、价格与 URL 是否忠于工具 Evidence 由 grounding 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
