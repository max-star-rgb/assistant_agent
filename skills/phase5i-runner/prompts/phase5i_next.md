继续执行下一个未完成的 Phase 5I task。

请先阅读该 task 的 Read first 文档。

执行规则：

- 只做当前 task Scope 内的内容。
- 不跨 task 实现。
- 优先 apply_patch 修改文件。
- 不写入 API Key。
- 不提交真实用户记忆、真实媒体或大文件。
- 不接真实 Vector DB。
- 不做复杂 RAG 平台。
- 不实现 MCP / Skills。
- 默认测试离线。
- memory / trace / 日志 / API 输出必须脱敏。
- 当前 task 测试通过后，继续下一个 task。
- Task 100 完成后停止，不要进入 Phase 5J。
