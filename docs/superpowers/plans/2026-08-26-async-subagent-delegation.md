# 异步子 Agent Delegation 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `AssistantRootGraph` 的 fast/planning 分支中加入可跨轮次管理的后台 Agent delegation，并让每个后台任务运行在独立的 `assistant-worker-v1` thread/run 中。

**Architecture:** 保留 `AssistantRootGraph` 作为唯一用户会话入口；新增一个复用 fast agent 配置但只暴露显式 read Tool 的独立 worker graph。Supervisor 使用 Deep Agents 官方 `AsyncSubAgentMiddleware` 的 state、prompt 和五个 Tool schema，项目仅以窄 transport adapter 补齐当前上游未传播的 authenticated user 与 graph-bound thread 信息。

**Tech Stack:** Python 3.12、LangChain 1.3.15、LangGraph 1.2.x、Deep Agents 0.7.8、LangGraph Agent Server/SDK、pytest。

**Spec:** 本轮对话中已批准的“独立生命周期后台 Agent delegation”目标；按仓库 `AGENTS.md` 不额外制造重复设计 spec。

## Global Constraints

- 默认测试和实现验证使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。
- `task(description, subagent_type)` 继续只表示 planning 当前 run 内的同步临时子 Agent。
- 后台任务必须使用独立 `assistant-worker-v1` graph、thread 和 run；不得把 worker 静态嵌入 RootGraph。
- `async_tasks` 必须位于 RootGraph、fast 与 planning 共享的 checkpoint channel，且按 task ID 合并更新。
- worker 首版只暴露 metadata `effect=read` 的业务 Tool，不暴露 async delegation Tool，不支持递归委托。
- 复用官方 middleware；项目 adapter 只负责动态认证 header、graph-bound thread create 与 SDK 生命周期调用。
- 保留用户当前工作区中的既有未提交改动，不回滚或覆盖无关内容。

---

### Task 1: 固化官方 middleware 与共享 state 契约

**Files:**
- Create: `tests/tdd/async-subagent-delegation/test_async_delegation.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Create: `src/assistant_agent/agent_server/async_delegation.py`

**Interfaces:**
- Produces: `merge_async_tasks(current, update) -> dict[str, dict[str, JsonValue]]`
- Produces: `build_async_subagent_middleware() -> AsyncSubAgentMiddleware`

- [ ] 写失败测试：五个官方 Tool 名存在，fast/planning state 可合并同一任务的状态更新，middleware 使用 `assistant-worker-v1`。
- [ ] 运行 `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/async-subagent-delegation/test_async_delegation.py`，确认因实现缺失失败。
- [ ] 实现最小共享 reducer 和 middleware factory；用官方 `AsyncSubAgentMiddleware` 构造 Tool/state，只替换 SDK transport handler。
- [ ] 重跑同一测试，确认通过。

### Task 2: 装配 worker graph 与 fast/planning 异步工具

**Files:**
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/native_agent/planning_agent.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/agent_server/graph.py`
- Modify: `src/assistant_agent/agent_server/config.py`
- Modify: `langgraph.json`
- Test: `tests/tdd/async-subagent-delegation/test_async_delegation.py`

**Interfaces:**
- Produces: `AgentServerExecutionOwner.worker_graph`
- Produces: `native_worker_graph(runtime)` lifespan factory
- Produces: `WORKER_GRAPH_ID = "assistant-worker-v1"`

- [ ] 写失败测试：manifest 注册 worker graph；fast/planning 都暴露五个异步 Tool；worker 不暴露这些 Tool 且只拥有 read Tool。
- [ ] 运行临时 TDD 测试，确认缺少 worker graph/Tool 装配导致失败。
- [ ] 从同一模型、Skills backend、token counter 与 read-only inventory 编译 worker；root fast 和 planning 分别装配官方异步 middleware，planning 的同步 `task` 继续调用无递归能力的 worker。
- [ ] 注册 `native_worker_graph` 并把 worker graph 纳入 owner 生命周期。
- [ ] 重跑临时 TDD 测试，确认通过。

### Task 3: 保持 authenticated identity 与 graph binding

**Files:**
- Modify: `src/assistant_agent/agent_server/async_delegation.py`
- Modify: `src/assistant_agent/agent_server/auth.py`
- Test: `tests/tdd/async-subagent-delegation/test_async_delegation.py`
- Test: `tests/core/contract/test_gateway_contract.py`

**Interfaces:**
- Consumes: `ToolRuntime.server_info.user.identity`
- Produces: worker thread metadata `assistant_graph_id=assistant-worker-v1`

- [ ] 写失败测试：`start_async_task` 以当前 authenticated identity 创建 graph-bound thread/run；update/check/cancel 只能使用 Root state 中已跟踪 task。
- [ ] 运行测试，确认现有官方 transport 不传 identity/graph binding 而失败。
- [ ] 用同一 8089 Agent Server 的 SDK 客户端和逐请求 `X-Assistant-User` header 实现窄 transport；auth allowlist 增加 worker graph，并按 `assistant_id` 返回精确 graph filter。
- [ ] 重跑临时与 gateway 定向测试，确认通过。

### Task 4: 更新稳定不变量与 authority

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `tests/core/contract/test_gateway_contract.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Produces: LOOP-001、CTX-001、GATE-001 对后台 delegation 的稳定结构化契约。

- [ ] 更新已有 core 测试，保护 worker graph identity、Root `async_tasks` channel、fast/planning Tool 暴露、worker read-only/no-recursion 和同步 `task` 保持。
- [ ] 更新 owner authority，明确同步 `task`、异步 Agent Protocol delegation、状态所有权和 mutation 边界。
- [ ] 运行 runtime/context/gateway 三个定向 core 文件。
- [ ] 运行 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`。

### Task 5: 完整验证、reload 与提交

**Files:**
- Verify only; do not add unrelated files.

**Interfaces:**
- Produces: 离线验证证据与只包含本任务文件的提交。

- [ ] 运行临时 TDD feature、runtime/context/gateway 定向 core 测试和文档 authority checker。
- [ ] 检查现有 8089 服务日志/health，确认 hot reload 后 manifest 和 graph 均可加载；不得另起第二个 dev server。
- [ ] 逐项审计目标：独立 graph/thread/run、fast/planning 管理 Tool、Root 共享状态、同步 task 不回归、worker 无递归和 read-only。
- [ ] 检查 `git diff`，仅暂存并提交本任务相关 hunks；不 push、不合并、不创建 PR。
