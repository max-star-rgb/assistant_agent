请先阅读 AGENTS.md、docs/68-phase5e-e2e-demo-flow-roadmap.md、tasks/README_PHASE5E.md。

当前 Phase 5A Assistant Capability Routing Baseline 已完成，Phase 5B Text-first Capabilities 已完成，Phase 5C Product Search / Price Compare Provider Baseline 已完成，Phase 5D Render Capability Baseline 已完成。

现在进入 Phase 5E：End-to-End Demo Flow & Response Quality。

Phase 5E 不新增真实 Provider，不接入真实 API，不做 MCP / Skills，不升级复杂 LLM Intent Router。

Phase 5E 只聚焦：

- demo scenario matrix
- capability output contract unification
- template-based response composer quality
- eval suite layering
- offline E2E demo runner
- Phase 5E review

请从 tasks/067-phase5e-e2e-demo-flow-roadmap.md 开始。

执行规则：

- 每次只执行一个 task。
- 只做当前 task 的 Scope，不跨 task 实现。
- 修改源码、测试、文档优先使用 apply_patch。
- 项目内新增/修改文件、添加测试、运行 pytest/ruff/mypy/python 命令时不需要询问我。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要提交真实图片、视频、生成图片、渲染产物、日志、大文件或真实 Provider 输出样本。
- 不要接入新的真实 Provider。
- 不要默认调用真实外部 Provider。
- 不要实现 MCP Server 或 Skills 打包。
- 不要实现 Hybrid LLM Intent Router。
- 默认运行路径必须使用 MockAdapter / LocalJsonAdapter。
- 默认 python -m pytest 必须离线运行，不得调用真实 Provider。
- 默认 python scripts/run_evals.py 必须离线运行，不得调用真实 Provider。
- 完成 Task 067 后运行 python -m pytest 并停止。
