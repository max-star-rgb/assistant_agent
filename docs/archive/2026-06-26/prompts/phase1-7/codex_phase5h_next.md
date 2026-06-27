继续执行下一个未完成的 Phase 5H task。

请先阅读该 task 的 Read first 文档。

执行规则：

- 只做当前 task Scope 内的内容。
- 不跨 task 实现。
- 优先 apply_patch 修改文件。
- 不写入 API Key。
- 不新增真实 Provider。
- 不调用真实 Provider。
- 不实现 MCP / Skills。
- 不实现 Memory Hardening。
- 默认测试离线。
- 所有错误、trace、日志、API 输出必须脱敏。
- 测试通过后停止。
- 告诉我下一个 task 是什么。
