# Context Token 计量收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有影响模型上下文窗口准入和内容选择的决策使用目标模型 tokenizer，同时保留字符、字节和条目数作为局部安全及观测指标。

**Architecture:** `ContextTokenCounter` 独立于 compactor 生命周期，由 runtime 在真实模式且配置本地 tokenizer 时创建。`ContextService` 始终在 counter 可用时执行完整 compiled request preflight；compactor 不可用时，soft trigger 只记录状态，hard trigger 阻断调用。Conversation recent window 接受同一 counter，缺失时保留明确标记为 estimate 的离线回退。

**Tech Stack:** Python、Pydantic、Hugging Face `tokenizers`、pytest。

## Global Constraints

- 默认 pytest 和本地验证使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- tokenizer 只从本地资产加载，不联网下载。
- byte/char/item 安全限制和字符观测字段不机械替换为 token。
- 保持 native tool call/result 因果配对，不按 token 直接截断结构化请求。
- 只修改本任务相关文件，不覆盖工作树中已有视觉记忆改动。

---

### Task 1: Token preflight 与 compactor 解耦

**Files:**
- Modify: `src/assistant_agent/context/token_counter.py`
- Modify: `src/assistant_agent/context/service.py`
- Test: `tests/core/integration/test_context_lifecycle.py`

**Interfaces:**
- Consumes: `ProviderConfig.context_tokenizer_path`、`ContextTokenCounter.count_chat_request()`。
- Produces: 可独立启用的 token accounting，以及无 compactor 时的 hard-limit admission failure。

- [x] 添加失败测试：real 模式关闭 compactor但配置 tokenizer 时仍创建 counter；无 compactor 的 hard preflight 阻断 Provider 请求。
- [x] 显式运行测试并确认因旧的 compactor 耦合行为失败。
- [x] 修改 counter factory 和 `ContextService.preflight()` 的能力判断。
- [x] 重跑定向测试并确认通过。

### Task 2: Conversation window 使用目标 tokenizer

**Files:**
- Modify: `src/assistant_agent/context/conversation.py`
- Modify: `src/assistant_agent/runtime/assistant_run_service.py`
- Test: `tests/core/integration/test_context_lifecycle.py`

**Interfaces:**
- Consumes: `ContextTokenCounter.count_text(value: str) -> int`。
- Produces: `select_conversation_window(..., token_counter=...)` 及可区分 tokenizer/estimate 的结构化计量来源。

- [x] 添加失败测试：注入的 counter 决定 recent transcript 选择，不能退回字符启发式估算。
- [x] 显式运行测试并确认因 API 缺失或选择结果错误而失败。
- [x] 将 runtime counter 贯穿 conversation preparation，并让 conversation selection 优先使用它。
- [x] 保留 counter 缺失时的 deterministic estimate，但不得把它标成 tokenizer-backed。
- [x] 重跑定向测试并确认通过。

### Task 3: 文档、回归与差异审计

**Files:**
- Modify: `docs/context_engineering_status.md`

**Interfaces:**
- Consumes: Task 1、Task 2 的最终行为。
- Produces: 与源码一致的 token accounting、fallback 和局部字符边界说明。

- [x] 更新权威文档，说明计数与压缩能力独立、estimate 的非权威性质。
- [x] 运行 `CTX-001` 负责文件 `tests/core/integration/test_context_lifecycle.py`。
- [x] 运行格式/静态检查中仓库已有且与改动直接相关的最小命令。
- [x] 审查 `git diff`，确认未覆盖用户已有改动且没有把字符安全边界错误替换为 token。
