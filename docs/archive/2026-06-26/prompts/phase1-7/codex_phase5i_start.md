请先阅读 AGENTS.md、docs/99-phase5i-memory-hardening-roadmap.md、tasks/README_PHASE5I.md。

当前 Phase 5H Provider Safety / Retry / Cost / Trace Query 已完成。现在进入 Phase 5I：Memory Hardening。

Phase 5I 不新增真实 Provider，不接外部 memory service，不接 Vector DB，不做复杂 RAG 平台，也不做 MCP / Skills。

Phase 5I 只聚焦：

- MemoryItem / MemoryQuery / MemorySearchResult
- MemoryStore boundary
- Retrieval ranking
- Memory context builder
- Memory write policy
- Lifecycle / delete
- Privacy / user isolation
- Memory eval / API / demo coverage
- Phase 5I review

请从 tasks/094-phase5i-memory-hardening-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实用户记忆、真实媒体、日志、大文件或 provider raw response。
- 不要接真实 Vector DB。
- 不要做复杂 RAG 平台。
- 不要实现 MCP Server 或 Skills 打包。
- 默认运行路径必须使用 InMemoryStore / JsonlMemoryStore。
- 默认 python -m pytest 必须离线运行，不得调用外部 memory service。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用外部 memory service。
- 默认 python scripts/run_demo_flows.py 必须离线运行，不得调用外部 memory service。
- 所有 memory、trace、日志、API 输出都不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response 或敏感文件路径。
- 完成 Task 094 后运行 python -m pytest 并停止。
