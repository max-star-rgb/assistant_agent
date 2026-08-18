# 原生 Memory 热路径与冷路径实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将主图改成每个 chat run 只 recall 一次，并通过 Agent Server SDK 在同一 thread 上调度 30 分钟后的独立 memory graph run。

**Architecture:** `assistant-native-v1` 直接编排 `memory_recall -> execution_router -> fast/planning -> refresh_memory_extraction`；`assistant-memory-v1` 独立执行 `memory_extract`。回答后的 refresh 使用当前 conversation thread 精确 list/cancel/create，delayed run 使用 `after_seconds=1800` 与 `multitask_strategy="enqueue"`；不使用 ReflectionExecutor、自定义 timer 或队列。

**Tech Stack:** Python 3.12、LangGraph `StateGraph`、LangChain `create_agent`、LangGraph Agent Server SDK、LangMem、pytest。

## Global Constraints

- 不修改 `src/assistant_agent/media/visual_perception/**`。
- 不回滚、不覆盖工作区用户或其他 Codex 的改动，不提交本任务文件。
- fast 继续使用官方 `create_agent`；planning 继续使用官方 `StateGraph` 并复用同一个 fast agent。
- recall 每个顶层 chat run 恰好一次；planning workers 不重复 recall。
- extract 只存在于独立 `assistant-memory-v1` delayed run，不进入回答关键路径。
- debounce 默认值固定为 1800 秒；项目通过 Agent Server SDK list pending、按 metadata rollback 旧 Memory run、再 enqueue 新 run，不增加 timer、Redis queue、后台线程或 session manager。
- 用户身份只来自 authenticated runtime，不进入公开 input/configurable。

---

### Task 1：用测试锁定真实编译拓扑与调度契约

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Create: `tests/tdd/native-memory-service/test_delayed_extraction.py`

**Interfaces:**
- Consumes: `build_assistant_root_graph(...)`、`build_memory_extraction_graph(...)`、Agent Server graph factories。
- Produces: 可观察契约——主图没有 compiled Memory/Agent 包装子图；recall 每顶层 run 一次；schedule 使用同一 thread、memory assistant、1800 秒和 enqueue；memory graph 只 commit。

- [x] 修改 LOOP-001：主图节点固定包含 `memory_recall`、`execution_router`、`fast_agent`、`planning_graph`、`refresh_memory_extraction` 与 START/END。
- [ ] 修改 MEMORY-001：fast/planning 都只 recall 一次，schedule 不调用 commit；独立 memory graph 只调用一次 commit 且不调用 Agent。
- [ ] 新增 delayed-run probe，monkeypatch SDK client 并断言：

```python
assert request == {
    "thread_id": "thread-sentinel",
    "assistant_id": "assistant-memory-v1",
    "input": {"messages": expected_messages},
    "after_seconds": 1800,
    "multitask_strategy": "enqueue",
}
```

- [ ] 运行目标测试，确认旧实现因仍有 `run_type`、`AssistantMemoryGraph` 且没有 SDK 调度而 RED。

### Task 2：拆分主图热路径与独立 Memory Graph

**Files:**
- Modify: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/native_agent/memory_graph.py`
- Delete: `src/assistant_agent/native_agent/agent_graph.py`
- Modify: `src/assistant_agent/native_agent/memory.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/__init__.py`

**Interfaces:**
- Produces: `build_assistant_root_graph(memory_backend, fast_agent, planning_graph, extraction_delay_seconds=1800)`。
- Produces: `build_memory_extraction_graph(backend)`。
- Produces: `refresh_memory_extraction_node(...)`。
- Removes: `AssistantRunType`、`run_type`、`build_agent_graph`、主图中的 `AssistantMemoryGraph`。

- [x] 将主图直接编排为 START → `memory_recall` → `execution_router` → fast/planning → `refresh_memory_extraction` → END。
- [ ] 为 recall 保留原生 RetryPolicy；最终失败 handler 更新 degraded snapshot 并跳到 `execution_router`。
- [ ] 调度节点使用 `langgraph_sdk.get_client()` 创建 delayed run，参数严格匹配 Task 1；只等待调度请求，不等待 memory graph。
- [ ] 为调度节点配置原生 retry/error handler；失败后直接 END，保留已经生成的标准 `AIMessage`。
- [ ] 将 `memory_graph.py` 收窄为 START → `memory_extract` → END，公开严格 messages input。
- [ ] LangMem commit 处理完整 conversation messages；Mem0 adapter 保持其 completed-turn 边界。
- [ ] 运行 Task 1 测试并确认 GREEN。

### Task 3：注册双 Graph 并共享进程级 composition

**Files:**
- Modify: `langgraph.json`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/agent_server/graph.py`
- Modify: `.env.example`
- Modify: `src/assistant_agent/config/__init__.py`

**Interfaces:**
- Produces: `AgentServerExecutionOwner.assistant_graph` 与 `.memory_graph`，共享 model/backend/store；MCP 仅为 assistant graph 装配一次。
- Produces: `native_assistant_graph(runtime)` 与 `native_memory_graph(runtime)` 两个 factory。
- Produces: `ProviderConfig.memory_extraction_delay_seconds: int = 1800`，环境变量 `MEMORY_EXTRACTION_DELAY_SECONDS`。

- [ ] 在配置解析中加入严格正整数 delay，默认 1800；同步 `.env.example`。
- [ ] composition 一次构造 backend，并分别编译 assistant graph 与 memory graph。
- [ ] graph factory 通过现有 async lock 复用同一 owner；两个 factory 返回各自 compiled graph。
- [ ] `langgraph.json` 注册 `assistant-native-v1` 和 `assistant-memory-v1`。
- [ ] 扩展 process-cache 测试，确认两个 factory 共享同一 owner、重复访问不重新 compose。

### Task 4：同步权威文档并验证真实 debounce 边界

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/memory_server_api_spec.md`
- Modify: `docs/authority.toml`（仅在 memory graph 新入口需要补 source_glob 时）

**Interfaces:**
- Documents: read hot path、write cold path、同 thread delayed run、30 分钟 enqueue debounce、双 graph 生命周期。

- [ ] 删除文档中的 `run_type=memory_extract` 与 `AssistantMemoryGraph` 主图描述。
- [ ] 明确 `Memory` 是领域边界而非必须整体嵌入的 compiled subgraph。
- [ ] 运行 mock core、临时 TDD、compileall 与 documentation authority validator。
- [ ] 重启唯一 8000 服务，验证 schemas 中主图没有 `run_type`，并确认两个 graph 均可加载且后续访问复用缓存。
- [ ] 真实 chat 只调用一次回答模型；trace 顺序为 recall → fast/planning → schedule，chat run 中没有 extract LLM。
- [ ] 使用短暂测试配置或 Agent Server run 查询验证连续同-thread chat 会替换尚未到期的 delayed memory run；正式默认仍为 1800 秒。
- [ ] 报告 Core invariant、Tests、真实 Provider 调用范围、视觉任务未纳入范围与任何 Agent Server 本地开发版限制。
