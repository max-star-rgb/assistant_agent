继续执行下一个未完成的 Phase 5C task。

请先阅读该 task 的 Read first 文档。

执行规则：

- 只做当前 task Scope 内的内容。
- 不跨 task 实现。
- 优先 apply_patch 修改文件。
- 不写入 API Key。
- 不提交大规模真实商品数据。
- 不写爬虫。
- 不处理登录、cookie、购买、下单、支付。
- 默认测试不联网。
- 测试通过后停止。
- 告诉我下一个 task 是什么。
- 保持实现轻量化。不要把商品搜索做成独立电商系统，不做爬虫、不做登录、不做支付、不接真实平台。优先完成 mock/local_json/
 http skeleton、search→compare 数据流、eval 和 API 覆盖。不要扩大 scope。