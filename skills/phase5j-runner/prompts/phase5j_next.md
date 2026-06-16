继续执行下一个未完成的 Phase 5J task。

请先阅读该 task 的 Read first 文档。

执行规则：

- 只做当前 task Scope 内的内容。
- 不跨 task 实现。
- 优先 apply_patch 修改文件。
- 不写入 API Key。
- 不发布远程 MCP 服务。
- 不新增真实 Provider。
- MCP tools 不得直接调用 Provider SDK。
- MCP tools 不得绕过 ProviderSafety / MemoryPrivacy / CapabilityValidator。
- Skills 不得包含真实数据或密钥。
- 所有 SKILL.md 必须包含 YAML frontmatter。
- 默认测试和 smoke 离线。
- 当前 task 测试通过后，继续下一个 task。
- Task 107 完成后停止，不要进入 Phase 6。
