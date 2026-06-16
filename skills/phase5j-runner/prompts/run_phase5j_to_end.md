请使用 skills/phase5j-runner/SKILL.md。

当前我要一次性完成 Phase 5J：MCP / Skills Packaging。

请从当前未完成的 Phase 5J task 开始，自动顺序执行到 Task 107 Phase 5J Review 结束。

任务顺序：

- 101 Phase 5J MCP / Skills Packaging Roadmap
- 102 MCP Tool Boundary and Contract Inventory
- 103 MCP Server Skeleton and Offline Tool Smoke
- 104 Skills Packaging Structure and Skill Templates
- 105 Skill Runbooks and Demo Flow Packaging
- 106 MCP / Skills Safety, Eval, and Docs Coverage
- 107 Phase 5J Review

如果某个 task 已经完成，请跳过并继续下一个未完成 task。

Prompt 使用规则：

- Task 101 使用 Phase 5J start prompt。
- Task 102-107 使用 Phase 5J next prompt。

本次临时允许连续执行多个 task，不需要每个 task 后等待我确认，但必须完成一个 task 后再进入下一个 task，并运行该 task 的验收命令。

执行规则：

- 只做到 Task 107，不要开始 Phase 6。
- 每个 task 只做自己的 Scope，不跨 task 扩展。
- 修改源码、测试、文档优先使用 apply_patch。
- 不要 retry without sandbox。
- 不要联网安装依赖。
- 不要写入 API Key。
- 不要创建包含真实密钥的 .env 或 .env.local。
- 不要发布远程 MCP 服务。
- 不要实现复杂 OAuth / 权限系统。
- 不要新增真实 Provider。
- 不要默认调用真实外部 Provider。
- 不要提交真实 Provider raw response、真实媒体、生成图、渲染产物、日志、大文件或真实 Provider 输出样本。
- MCP tools 必须复用 AgentGraphRuntime / ToolRegistry，不得直接调用 Provider SDK。
- MCP tools 不得绕过 ProviderSafety / MemoryPrivacy / CapabilityValidator。
- Skills 不得包含真实 API Key、真实用户数据或真实 Provider 输出。
- 所有 SKILL.md 必须包含 YAML frontmatter。
- 默认 python -m pytest 必须离线运行。
- 默认 python scripts/run_evals.py 必须离线运行。
- 默认 python scripts/run_demo_flows.py 必须离线运行。
- 默认 python scripts/smoke_mcp_tools.py 必须离线运行。
- 默认 python scripts/validate_skills.py 必须离线运行。
- 所有 MCP、Skills、trace、日志、API 输出都不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response 或敏感文件路径。

Task 107 完成后停止，并给出执行摘要、测试结果、剩余问题和下一阶段建议。
