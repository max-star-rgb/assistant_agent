请先阅读 AGENTS.md、docs/91-phase5h-provider-safety-roadmap.md、tasks/README_PHASE5H.md。

当前 Phase 5G Video Understanding as External MLLM Capability 已完成。现在进入 Phase 5H：Provider Safety / Retry / Cost / Trace Query。

Phase 5H 不新增真实 Provider，不接入真实 API，不做 MCP / Skills，也不做 Memory Hardening。

Phase 5H 只聚焦：

- ProviderError taxonomy
- ProviderSafetyPolicy
- Retry / Fallback / Timeout policy
- ProviderCallBudget
- Trace query
- Redaction
- Provider safety eval / API coverage
- Phase 5H review

请从 tasks/087-phase5h-provider-safety-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实 Provider raw response、真实媒体、生成图、渲染产物、日志、大文件或真实 Provider 输出样本。
- 不要新增真实 Provider。
- 不要默认调用真实外部 Provider。
- 不要实现 MCP Server 或 Skills 打包。
- 不要实现 Memory Hardening。
- 默认运行路径必须使用 MockAdapter / LocalJsonAdapter。
- 默认 python -m pytest 必须离线运行，不得调用真实 Provider。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 Provider。
- 所有错误、trace、日志、API 输出都不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response 或敏感文件路径。
- 完成 Task 087 后运行 python -m pytest 并停止。
