请先阅读 AGENTS.md、docs/76-phase5f-hybrid-intent-router-roadmap.md、tasks/README_PHASE5F.md。

当前 Phase 5E End-to-End Demo Flow & Response Quality 已完成。现在进入 Phase 5F：Hybrid Intent Router & Planner Quality。

Phase 5F 不新增真实 Provider，不接入真实 API，不做 MCP / Skills。

Phase 5F 只聚焦：

- IntentDecision schema
- CapabilityValidator
- Rule Router confidence
- LLM Intent Router Adapter skeleton
- MockLLMIntentRouter
- Planner slot filling
- Router eval comparison
- Phase 5F review

请从 tasks/074-phase5f-hybrid-intent-router-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要接入新的真实 Provider。
- 不要默认调用真实 LLM。
- 不要让 LLM 直接执行工具。
- 不要实现 MCP Server 或 Skills 打包。
- 默认 router 必须仍为 rule。
- 默认 python -m pytest 必须离线运行，不得调用真实 LLM 或真实 Provider。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 LLM 或真实 Provider。
- MockLLMIntentRouter 可以用于测试，但不得访问网络。
- LLM 输出必须经过 IntentDecision schema 校验和 CapabilityValidator。
- 完成 Task 074 后运行 python -m pytest 并停止。
