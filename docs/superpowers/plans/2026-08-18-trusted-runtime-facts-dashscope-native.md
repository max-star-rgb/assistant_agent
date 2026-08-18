# 可信实时事实与 DashScope 原生模型实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每次原生 Graph run 冻结可信时间/默认地点，并让 qwen provider 通过 DashScope 原生 API 返回可投影的联网来源。

**Architecture:** 父图在 Memory recall 前增加 `capture_trusted_runtime_facts`，将结构化快照写入 checkpoint；fast/planning 的每次模型请求把 MemoryContext 与 TrustedRuntimeFacts 作为两条临时 HumanMessage 放在最后一条真实用户消息之前。qwen real provider 使用实现 `BaseChatModel` 的 DashScope 原生适配器，来源保存在对应 `AIMessage.response_metadata`，仅终态消息由入口投影给客户端。

**Tech Stack:** Python 3.11、LangChain `BaseChatModel`、LangGraph `StateGraph`、Pydantic v2、DashScope HTTP/SSE API、pytest。

**Spec:** `docs/context_engineering_status.md`（相邻权威：`docs/runtime-event-stream-architecture.md`、`docs/memory-service-architecture.md`、`docs/tool-calling-architecture.md`）

## Global Constraints

- 默认地点为上海，标记为 `deployment_default` 与 fallback，不能表述为已观测用户物理位置。
- capture 节点被 checkpoint 跳过时沿用快照；从更早 checkpoint replay 并重跑节点时重新采集，语义与 Memory recall 相同。
- 临时上下文不写入 `messages` state，不参与摘要或 Memory 提取；LangMem commit 额外剥离 provider metadata。
- qwen 的 `QWEN_CHAT_API_PROTOCOL=dashscope` 必须使用官方原生端点和字段；不静默回退 OpenAI-compatible。
- pytest 全程使用 mock/offline；不调用真实 Provider。

---

### Task 1: 冻结并注入可信实时事实

**Files:**
- Create: `src/assistant_agent/native_agent/runtime_facts.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/native_agent/planning_graph.py`
- Modify: `src/assistant_agent/native_agent/memory.py`
- Test: `tests/tdd/trusted-runtime-facts-dashscope-native/test_runtime_facts.py`

**Interfaces:**
- Produces: `TrustedRuntimeFacts`, `RuntimeLocation`, `capture_trusted_runtime_facts_node(state, *, clock=...)`、`TrustedRuntimeFactsMiddleware`。
- Consumes: `AssistantRootState`、`FastAgentState`、`PlanningState`、现有 `MemoryContextMiddleware`。

- [ ] **Step 1: 写失败测试**：断言 capture 产生带时区快照、父图 capture 位于 recall 前、临时消息顺序为 Memory → RuntimeFacts → 当前用户、planning worker 继承快照、LangMem 输入不含 provider metadata。
- [ ] **Step 2: 验证 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/trusted-runtime-facts-dashscope-native/test_runtime_facts.py`

  Expected: FAIL，缺少 `runtime_facts` 模块或状态字段。

- [ ] **Step 3: 最小实现**：新增冻结节点和渲染中间件，将快照透传到 planning 的 planner/worker/finalizer；Memory 临时文案改为“最后一条用户消息”；LangMem commit 使用去 metadata 的消息副本。
- [ ] **Step 4: 验证 GREEN**：重复 Step 2 命令并确认 PASS。

### Task 2: 接入 DashScope 原生 BaseChatModel

**Files:**
- Create: `src/assistant_agent/providers/dashscope_langchain.py`
- Modify: `src/assistant_agent/providers/dashscope_chat.py`
- Modify: `src/assistant_agent/native_agent/providers.py`
- Test: `tests/tdd/trusted-runtime-facts-dashscope-native/test_dashscope_langchain.py`

**Interfaces:**
- Produces: `DashScopeNativeChatModel(BaseChatModel)`，支持 `bind_tools`、同步/异步生成和流式输出。
- Consumes: `DashScopeHttpTransport`、官方 Generation API message/tool/search/SSE 格式。

- [ ] **Step 1: 写失败测试**：使用完整官方结构 fake response，断言原生 URL、`result_format=message`、联网来源参数、tool message/call 映射、usage 与 `provider_search_sources` 进入 AIMessage；断言 qwen factory 依据 protocol 选择原生模型且配置缺失时 fail-fast。
- [ ] **Step 2: 验证 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/trusted-runtime-facts-dashscope-native/test_dashscope_langchain.py`

  Expected: FAIL，缺少 `DashScopeNativeChatModel`。

- [ ] **Step 3: 最小实现**：按官方 API 编解码 messages/tools/search/source/usage/tool calls；来源只附着当前 AIMessage；协议或 Provider 错误抛出脱敏异常；保留非 qwen/OpenAI-compatible 现有路径。
- [ ] **Step 4: 验证 GREEN**：重复 Step 2 命令并确认 PASS。

### Task 3: 仅投影终态来源

**Files:**
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Test: `tests/tdd/trusted-runtime-facts-dashscope-native/test_terminal_sources.py`

**Interfaces:**
- Consumes: 终态 `AIMessage.response_metadata.provider_search_sources`。
- Produces: `native_response_from_state()` 中 provider-neutral `citations`，供既有 `urlCitationAnnotationsV1` wire 投影使用。

- [ ] **Step 1: 写失败测试**：历史 AIMessage 有来源、终态无来源时不得聚合；终态含 `[1]` 时只投影终态来源及其文本 span；无角标或无效 URL 不生成 annotation。
- [ ] **Step 2: 验证 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/trusted-runtime-facts-dashscope-native/test_terminal_sources.py`

  Expected: FAIL，终态响应没有 `citations`。

- [ ] **Step 3: 最小实现**：从最新终态 AIMessage 读取来源并将实际角标位置转换为 `url_citation` annotations；不修改 AIMessage 正文。
- [ ] **Step 4: 验证 GREEN**：重复 Step 2 命令并确认 PASS。

### Task 4: 同步权威与完成验证

**Files:**
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Produces: 与源码一致的 RuntimeFacts、Memory 临时消息、DashScope source 生命周期说明。

- [ ] **Step 1: 更新 owner authority**：只更新本次行为涉及的现状与边界，不修改用户已有无关段落。
- [ ] **Step 2: 运行 feature TDD 集合**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/trusted-runtime-facts-dashscope-native`

- [ ] **Step 3: 运行相关现有核心回归**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/core/integration/test_memory_lifecycle.py tests/core/contract/test_gateway_contract.py`

- [ ] **Step 4: 检查 authority**

  Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`

- [ ] **Step 5: 等待并验证 8089 hot reload**：检查既有服务日志/健康状态，不启动第二套 dev server。

