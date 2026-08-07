"""Grader for constraint-aware multi-day travel planning."""

from evals.agent.batch_grading import grader_for_response_quality


RESPONSE_QUALITY_RUBRIC = """
通过条件：
1. 比较两个酒店的每晚价、三晚总价口径，以及去杭州东站、灵隐寺和中国丝绸博物馆的关键通勤；说明东站便利、景点通勤与慢节奏之间的取舍，并推荐湖滨慢行酒店。
2. 只交付工具返回的对应 OTA 链接，明确链接不代表锁价、预订成功或最终成交。
3. 按 10 月 2 日至 5 日逐天给出上午、下午和晚上安排；抵达日使用车站到首选酒店的路线证据，返程日使用首选酒店到车站的路线时长安排明确出发时间，并为 17:00 返程保留充足进站缓冲。
4. 行程覆盖西湖、灵隐寺和中国丝绸博物馆，并尽量把相邻区域安排在同一天，不能与 10 月 5 日 17:00 杭州东站返程冲突。
5. 把酒店与路线工具结果列为已核验信息；没有可识别网页来源的开放、预约、票务和节假日规则必须列为待确认，不能编造官方结论或链接。
6. 旅行日期超出当前短期天气证据时，不生成具体逐日天气；给出出发前复查与室内外互换建议即可。
判定边界：具体酒店价格、路线时间和 URL 是否忠于工具 Evidence 由 grounding 判断；是否完成 Skill、住宿、地点和路线调用由 tool_execution Rule 判断。
""".strip()


grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
