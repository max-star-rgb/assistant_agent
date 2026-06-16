请使用 skills/phase5i-runner/SKILL.md。

当前我要一次性完成 Phase 5I：Memory Hardening。

请从当前未完成的 Phase 5I task 开始，自动顺序执行到 Task 100 Phase 5I Review 结束。

任务顺序：

- 094 Phase 5I Memory Hardening Roadmap
- 095 Memory Data Model and Store Boundary
- 096 Memory Retrieval Ranking and Context Builder
- 097 Memory Write Policy and Lifecycle
- 098 Memory Privacy and User Isolation
- 099 Memory Eval / API / Demo Coverage
- 100 Phase 5I Review

如果某个 task 已经完成，请跳过并继续下一个未完成 task。

Prompt 使用规则：

- Task 094 使用 Phase 5I start prompt。
- Task 095-100 使用 Phase 5I next prompt。

本次临时允许连续执行多个 task，不需要每个 task 后等待我确认，但必须完成一个 task 后再进入下一个 task，并运行该 task 的验收命令。

执行规则：

- 只做到 Task 100，不要开始 Phase 5J。
- 每个 task 只做自己的 Scope，不跨 task 扩展。
- 修改源码、测试、文档优先使用 apply_patch。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实用户记忆、真实媒体、日志、大文件或 provider raw response。
- 不要调用真实外部 Provider。
- 不要调用外部 memory service。
- 不要接真实 Vector DB。
- 不要做复杂 RAG 平台。
- 不要实现 MCP Server 或 Skills 打包。
- 默认运行路径必须使用 InMemoryStore / JsonlMemoryStore / MockAdapter / LocalJsonAdapter。
- 默认 python -m pytest 必须离线运行。
- 默认 python scripts/run_evals.py 必须离线运行。
- 默认 python scripts/run_demo_flows.py 必须离线运行。
- 所有 memory、trace、日志、API 输出都不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response 或敏感文件路径。

Task 100 完成后停止，并给出执行摘要、测试结果、剩余问题和下一阶段建议。
