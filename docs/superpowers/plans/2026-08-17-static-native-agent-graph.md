# 静态原生 Agent 大图实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将生产入口改成进程内只装配一次的静态 `AssistantRootGraph`，直接组合 `Agent` 与 `Memory` 子图，并把长期记忆提取移出普通聊天 run。

**Architecture:** Agent Server 继续拥有 thread、run、queue、checkpoint、Store 与 stream；进程第一次取图时装配模型、本地 Tool、MCP Tool 和编译图，后续 factory 调用只返回同一图。普通 `chat` run 执行 `Memory.recall -> Agent`，显式 `memory_extract` 后台 run 只执行 `Memory.extract`。

**Tech Stack:** Python 3.12、LangGraph `StateGraph`、LangChain `create_agent`、LangGraph Agent Server、LangMem、pytest。

## 全局约束

- 不修改 `src/assistant_agent/media/visual_perception/**`，不承担视觉生命周期任务。
- 保留工作区全部用户及其他 Codex 改动，不回滚、不批量格式化、不提交。
- fast 必须继续由官方 `create_agent` 创建；planning 必须继续使用官方 `StateGraph` 并复用同一个 fast 子图。
- Tool 继续使用 `BaseTool -> ToolNode`，MCP 继续使用官方 `MultiServerMCPClient`。
- 不新增 Agent Runtime、session manager、任务队列、checkpoint facade 或 Tool executor。
- pytest 只使用 mock/offline；真实 Provider 仅用于最终显式 Studio 验证。

---

### Task 1：锁定新的核心图契约

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Create: `tests/tdd/static-native-agent-graph/test_process_graph_cache.py`

**Interfaces:**
- Consumes: `AgentServerExecutionOwner.compose(store=...)`、Agent Server graph factory。
- Produces: 可观察契约——顶层直接节点为 `agent`、`memory`；chat 每 run recall 且不 extract；`memory_extract` run 只 extract；同一进程重复取图返回同一编译图。

- [ ] 修改现有 LOOP-001、MEMORY-001、BOOT-001 测试，使用 probe backend 和真实编译图验证上述行为。
- [ ] 显式运行目标测试并确认因旧图仍包含 `memory_commit`、未分层或重复装配而失败。

### Task 2：实现 Agent 与 Memory 原生子图

**Files:**
- Create: `src/assistant_agent/native_agent/agent_graph.py`
- Create: `src/assistant_agent/native_agent/memory_graph.py`
- Modify: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/memory.py`
- Modify: `src/assistant_agent/native_agent/__init__.py`

**Interfaces:**
- Produces: `build_agent_graph(fast_agent, planning_graph)`。
- Produces: `build_memory_graph(memory_backend)`。
- Produces: `AssistantRunType = Literal["chat", "memory_extract"]`，公开输入默认 `chat`。
- Produces: 顶层 `build_assistant_root_graph(memory_graph=..., agent_graph=...)`。

- [ ] Agent 子图只根据结构化 `execution_mode` 路由到既有 fast/planning compiled graph。
- [ ] Memory 子图只根据结构化 `run_type` 路由到 recall/extract node。
- [ ] 顶层静态图先执行 Memory；chat 后进入 Agent，memory_extract 后直接 END。
- [ ] 运行 Task 1 测试并确认 GREEN。

### Task 3：进程内复用同一个编译图

**Files:**
- Modify: `src/assistant_agent/agent_server/graph.py`
- Modify: `src/assistant_agent/agent_server/services.py`（只修改静态装配所需逻辑，保留视觉相关改动）
- Modify: `src/assistant_agent/agent_server/media_app.py`（仅在没有并发冲突时接入官方 lifespan shutdown）

**Interfaces:**
- Produces: Agent Server async factory 重复调用返回同一 `CompiledStateGraph`。
- Produces: 并发首次取图只执行一次 `AgentServerExecutionOwner.compose()`。
- Produces: 进程 shutdown 关闭一次 composition owner。

- [ ] 用 async lock/task 防止并发首次取图重复装配。
- [ ] factory 不再为 schema/history/run 创建独立 owner。
- [ ] MCP discovery 因 owner 复用而每个 worker 进程只执行一次。
- [ ] 显式运行 process-cache TDD 并确认 GREEN。

### Task 4：权威文档与验证

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/tool-calling-architecture.md`（仅当 MCP 生命周期表述需要同步）

**Interfaces:**
- Documents: 顶层 Agent/Memory 子图、chat/memory_extract 路由、每 run recall、独立后台 extract、进程级静态装配。

- [ ] 运行 mock/offline 定向 core 与临时 TDD。
- [ ] 运行 `scripts/check_documentation_authority.py --repo-root .` 并区分本次问题与既有 stale path。
- [ ] 重启唯一 8000 服务，通过 Studio 发起真实 chat run。
- [ ] 核对 LangSmith trace：Graph load 不再每请求重复；chat trace 只有主回答 LLM，没有 LangMem extract；recall 每 run 一次。
- [ ] 报告 Core invariant、Tests、真实 Provider 调用范围、未解决的 thread-end 触发边界以及视觉任务未纳入范围。
