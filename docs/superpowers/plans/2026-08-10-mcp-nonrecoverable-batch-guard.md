# MCP 不可恢复错误与批量调用熔断实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 正确识别 Gmail OAuth 未授权，并阻止同一 Provider turn 中后续相同 Tool 的无效执行。

**Architecture:** MCP/Email adapter 负责把远端结构化错误或兼容文本归一化为稳定 Provider 错误；assistant loop 在批量调用的每次实际执行前重新读取 `LoopGuard`，只阻止已经报告不可恢复错误的相同 Tool，其他 Tool 继续执行。Google 授权使用现有真实 MCP 配置单独触发，不进入 pytest。

**Tech Stack:** Python 3.12、Pydantic、LangGraph assistant loop、MCP Python SDK、pytest。

## Global Constraints

- 不修改 Skill、Tool catalog、Provider-native 联网或前三个根因相关实现。
- pytest 固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不访问网络或真实 Provider。
- 不输出或提交 OAuth URL、token、`.env` 或真实用户数据。
- 保持 native tool call/result 的 `provider_tool_call_id` 因果配对。

---

### Task 1: Gmail OAuth 错误归一化

**Files:**
- Modify: `tests/tdd/mcp-tool-failure-recovery/test_mcp_tool_failure_recovery.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/email_access/backend.py`

**Interfaces:**
- Consumes: MCP `ToolResult.error`、`model_observation`、`data.structured_content`
- Produces: `EmailProviderError(code="provider_auth_failed", recoverable=False)`

- [ ] 将 OAuth 回归测试改成 trace 中没有稳定错误码前缀的真实兼容文案。
- [ ] 运行定向测试并确认因得到 `provider_execution_failed` 而失败。
- [ ] 实现结构化错误优先、兼容文本兜底的 `_mcp_failure_code(result: ToolResult)`。
- [ ] 运行 `tests/tdd/mcp-tool-failure-recovery` 并确认通过。

### Task 2: 同批 Tool call 执行前重新熔断

**Files:**
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`

**Interfaces:**
- Consumes: `LoopGuard.nonrecoverable_failure_already_seen(tool_name)`
- Produces: `nonrecoverable_tool_retry_blocked` rejected observation，且不进入 `ToolExecutor`

- [ ] 使用通用 Probe Tool 添加同批不同输入回归测试，证明第一条不可恢复失败后第二条不会执行。
- [ ] 添加混合 batch 断言，证明不同 Tool 不受影响。
- [ ] 运行定向测试并确认现状错误地执行第二条相同 Tool。
- [ ] 在每条 pending call 执行前应用最新 Tool guard，并保留 Provider call/result 配对。
- [ ] 运行定向 core/TDD 测试并确认通过。

### Task 3: 文档、验证与 Google 授权

**Files:**
- Review only: `docs/tool-calling-architecture.md`

**Interfaces:**
- Consumes: 当前 authority 已声明的 batch execution-boundary 和 nonrecoverable contract
- Produces: 无重复 authority 文案；本机 Google OAuth 会话由用户在浏览器完成

- [ ] 复核 authority 是否已覆盖目标行为；只有存在漂移才修改。
- [ ] 运行最小 core/TDD 验证和 `scripts/check_documentation_authority.py --repo-root .`（仅当 authority 被修改）。
- [ ] 通过真实 Gmail MCP 调用取得授权入口并在本机打开，等待用户完成授权。
- [ ] 授权后执行一次最小只读 Gmail 搜索，确认不再返回认证错误。

