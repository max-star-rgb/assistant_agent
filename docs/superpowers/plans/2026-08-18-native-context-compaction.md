# 原生上下文压缩实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留 LangChain 原生 `SummarizationMiddleware`，将压缩策略改为 75% 触发、15% 保留，并用 DeepSeek V4 Flash 官方 tokenizer 计数且完整摘要被淘汰的消息。

**Architecture:** 不新增 Graph node、交互边界状态或自定义对话循环。通过官方 middleware 的 `token_counter` 与 `trim_tokens_to_summarize=None` 扩展点完成计数和完整摘要，继续由其原生安全切点保护 AI tool-call 与 ToolMessage 配对。

**Tech Stack:** Python 3.12、LangChain 1.3、LangGraph 1.2、Transformers/Fast Tokenizer、pytest。

**Spec:** 本轮用户确认的 75/15、原生安全切点及 DeepSeek V4 Flash tokenizer 方案。

## Global Constraints

- 不新增 Graph node 或自定义 interaction/run 状态。
- 默认触发比例为 `0.75`，目标保留比例为 `0.15`，并允许现有环境变量覆盖。
- 使用 `deepseek-ai/DeepSeek-V4-Flash` 官方 tokenizer 与消息编码格式。
- 默认测试和运行保持 mock/offline；不得调用真实 Provider。

---

### Task 1: Token 计数适配器与配置接线

**Files:**
- Create: `src/assistant_agent/context/deepseek_v4_tokens.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Test: `tests/tdd/native-context-compaction/test_context_compaction.py`

**Interfaces:**
- Consumes: LangChain 标准 message 列表、Provider model ID 和本地 tokenizer 资产。
- Produces: `build_context_token_counter(...) -> TokenCounter`，以及传入 `build_fast_agent` 的 trigger/target 配置。

- [ ] **Step 1: 写失败测试**

覆盖配置比例传递、官方 tokenizer 计数器可调用，以及缺少 tokenizer 资产时显式失败而非退回字符估算。

- [ ] **Step 2: 运行测试确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-context-compaction`

- [ ] **Step 3: 实现最小代码**

实现 tokenizer 缓存装载、LangChain message 到 DeepSeek V4 OpenAI-style message 的转换，并将 75/15 和计数器传入官方 middleware。

- [ ] **Step 4: 运行测试确认 GREEN**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-context-compaction`

### Task 2: 完整摘要与权威文档同步

**Files:**
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `docs/context_engineering_status.md`
- Test: `tests/tdd/native-context-compaction/test_context_compaction.py`

**Interfaces:**
- Consumes: 官方 `SummarizationMiddleware` 构造参数。
- Produces: `trim_tokens_to_summarize=None`，保持官方 AI/Tool 安全切点。

- [ ] **Step 1: 扩展失败测试**

断言长对话摘要不再经过默认 4K 局部裁剪，且 middleware 仍为官方实现。

- [ ] **Step 2: 运行测试确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-context-compaction`

- [ ] **Step 3: 实现并同步文档**

设置 `trim_tokens_to_summarize=None`，更新 context authority 中的 75/15、tokenizer 和原生切点说明。

- [ ] **Step 4: 完成定向验证**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-context-compaction tests/core/integration/test_context_lifecycle.py`

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`
