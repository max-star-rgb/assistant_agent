# 原生 Graph Identity 与 Memory Middleware 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留官方 `owner` 隔离与 30 分钟 Memory 提取策略，删除重复 `assistant_graph_id` 和外层 `AssistantRootGraph`。

**Architecture:** `assistant-native-v4` 直接注册 `create_deep_agent` 编译的 `AssistantAgent`。`MemoryLifecycleMiddleware` 在 `before_agent` 完成 recall、在 `after_agent` rollback/enqueue 延迟提取；真正 extraction 继续由独立 `assistant-memory-v1` 执行。Thread 身份只使用 Agent Server 原生 `metadata.graph_id`，`owner` 继续作为官方 metadata auth filter。

**Tech Stack:** Python 3.12、LangGraph Agent Server、LangChain Agent Middleware、Deep Agents、pytest。

**Spec:** `docs/agent-server-architecture.md`、`docs/runtime-event-stream-architecture.md`、`docs/memory-service-architecture.md`（本次同步更新为已批准目标）

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- 保留 `owner`；原生 `metadata.graph_id` 是唯一 thread graph identity。
- `enable_memory=false` 同时关闭 recall 和 delayed extraction。
- recall、refresh、独立 extraction 都最多尝试三次，异常不得吞掉。
- extraction 默认 `after_seconds=1800`，在独立 `assistant-memory-v1` graph 执行。
- 不回滚当前 worktree 中已有的 Prompt、Skills 与 Todo 文案修改。

---

### Task 1: 统一原生 thread graph identity

**Files:**
- Modify: `src/assistant_agent/agent_server/client.py`
- Modify: `src/assistant_agent/agent_server/auth.py`
- Modify: `src/assistant_agent/coding/backend.py`
- Modify: `tests/core/contract/test_gateway_contract.py`
- Modify: `tests/tdd/unified-assistant-agent/test_client_contract.py`
- Modify: `tests/tdd/unified-assistant-agent/test_read_only_worker.py`

**Interfaces:**
- Consumes: Agent Server 写入 thread metadata 的原生 `graph_id`。
- Produces: client/auth/backend 只读写 `graph_id`，不再创建 `assistant_graph_id`。

- [ ] **Step 1: 写失败测试**

  将 auth filter、SDK thread fixture 和 workspace config 的 graph identity 期望改为 `graph_id`；新增 thread metadata update 携带 `graph_id` 时必须拒绝的断言。

- [ ] **Step 2: 运行失败测试**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/core/contract/test_gateway_contract.py tests/tdd/unified-assistant-agent/test_client_contract.py tests/tdd/unified-assistant-agent/test_read_only_worker.py`

  Expected: FAIL，因为生产代码仍写入或读取 `assistant_graph_id`。

- [ ] **Step 3: 最小实现**

  删除自定义 graph identity metadata；`require_thread_graph_identity()` 读取 `metadata.graph_id`；create metadata 不重复写 graph ID；auth 只从原生 `graph_id` 判定，并拒绝 update 修改它；workspace backend 读取原生 `graph_id`。

- [ ] **Step 4: 运行测试并确认通过**

  Run: 与 Step 2 相同。

---

### Task 2: 将 Memory lifecycle 迁入主 Agent middleware

**Files:**
- Modify: `src/assistant_agent/native_agent/assistant_agent.py`
- Modify: `src/assistant_agent/native_agent/memory.py`
- Delete: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/context.py`
- Modify: `src/assistant_agent/native_agent/__init__.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/tdd/unified-assistant-agent/test_unified_graph.py`

**Interfaces:**
- Consumes: `MemoryBackend`、`AssistantRunContext.enable_memory`、`Runtime.execution_info`、标准 messages。
- Produces: `MemoryLifecycleMiddleware(memory_backend, extraction_delay_seconds=1800)`；直接编译的 `AssistantAgent` 主图。

- [ ] **Step 1: 写失败测试**

  断言主图名为 `AssistantAgent`，直接包含 model/tools 与 `MemoryLifecycleMiddleware.before_agent/after_agent` 节点，不再包含 `assistant_agent` 子图或 `AssistantRootGraph`；复用现有行为测试验证 recall、关闭开关、rollback/enqueue、三次重试。

- [ ] **Step 2: 运行失败测试**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/core/integration/test_memory_lifecycle.py tests/core/integration/test_runtime_lifecycle.py tests/tdd/unified-assistant-agent/test_unified_graph.py`

  Expected: FAIL，因为 Memory lifecycle 仍在外层 StateGraph。

- [ ] **Step 3: 最小实现**

  复用现有 recall 与 refresh 函数体，迁入一个 `AgentMiddleware`；用 `RunnableLambda(...).with_retry(stop_after_attempt=3, wait_exponential_jitter=False)` 保留三次重试。`build_assistant_agent()` 接收 Memory backend/delay 并注册 middleware；composition 直接把返回值作为 owner graph。删除无消费者的 root graph/state/export。

- [ ] **Step 4: 运行测试并确认通过**

  Run: 与 Step 2 相同。

---

### Task 3: 同步权威并完成验证

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/authority.toml`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: Task 1、2 的实际源码行为。
- Produces: owner authority、测试 invariant 与源码一致。

- [ ] **Step 1: 更新 authority 与 invariant**

  移除 `AssistantRootGraph`、`assistant_graph_id` 和父图固定节点表述；保留 `owner`、30 分钟 extraction、独立 Memory graph 和三次重试。

- [ ] **Step 2: 运行完整相关验证**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q`

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/unified-assistant-agent`

  Run: `python scripts/check_documentation_authority.py --repo-root .`

- [ ] **Step 3: 验证 8089 hot reload**

  连接现有 `8089`，检查 assistants/graph schema 与一次 mock run；不得启动第二个 dev server。

- [ ] **Step 4: 审计并提交**

  用 `rg` 确认生产源码与当前 authority 不再出现 `assistant_graph_id` 或 `AssistantRootGraph`；只提交本任务相关文件，不包含既有用户修改与本计划文件。
