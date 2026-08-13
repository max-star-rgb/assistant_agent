# Agent Server 原生运行时重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保持 `/agent-service/v1` 媒体协议兼容，将所有生产 Assistant Graph 执行迁移到 Agent Server 原生 auth/thread/run/queue/checkpoint/Store，并删除重复 Gateway Runtime。

**Architecture:** Agent Server 通过 `langgraph.json` 加载 graph factory 和 custom FastAPI app。factory 每次原生 run 创建受治理的 worker 依赖并返回固定拓扑 StateGraph；可序列化 run context 与 Python service object 分离。custom route 仅把现有媒体 WebSocket 协议映射到公开 Agent Server SDK，不拥有第二套 run、queue 或 checkpoint。

**Tech Stack:** Python 3.12、LangGraph 1.2、Agent Server / `langgraph-cli[inmem]`、`langgraph-sdk`、FastAPI WebSocket、Pydantic 2、pytest mock/offline。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；真实 Provider 不进入 pytest 或结构迁移验收。
- `/agent-service/v1` 外部 envelope、消息名和稳定字段保持兼容。
- custom route 只调用公开 `langgraph_sdk`/HTTP API，不导入 `langgraph_api` 私有模块。
- Agent Server 是 auth、thread、run、queue、cancel、checkpoint 和 Store 的唯一生产权威。
- Graph 继续执行 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`，入口不得绕过治理链。
- `memory_context` 继续是冻结 State 快照；LangMem 使用 Agent Server Store，Mem0 由 memory node 直接调用。
- `publish_response` 先于 `memory_commit`；外部副作用保留最小幂等 ledger。
- 不回滚或提交工作区既有未跟踪文件 `docs/superpowers/plans/2026-08-13-langgraph-native-memory-nodes.md`。
- Agent Server/CLI 新依赖安装必须获得用户明确许可；没有真实 server 进程证据不得宣称 deployment probe 通过。

---

## 文件结构

- `langgraph.json`：Agent Server deployment 清单，只声明 graph、auth 和 custom app。
- `src/assistant_agent/agent_server/context.py`：严格、可序列化的 Assistant/run context schema。
- `src/assistant_agent/agent_server/services.py`：一次 worker run 的受治理依赖 owner 与关闭语义。
- `src/assistant_agent/agent_server/graph.py`：`ServerRuntime` graph factory；不绑定 checkpointer。
- `src/assistant_agent/agent_server/auth.py`：Agent Server custom auth 与 owner/tenant 过滤。
- `src/assistant_agent/agent_server/client.py`：custom route 使用的公开 SDK 薄客户端协议。
- `src/assistant_agent/agent_server/media_app.py`：挂载 `/agent-service/v1` 的 custom FastAPI app。
- `src/assistant_agent/agent_server/media_protocol.py`：从旧入口提取的纯媒体 schema/投影。
- `src/assistant_agent/agent_server/media_session.py`：connection/thread/run/delivery 最小关联，不含执行状态机。
- `tests/tdd/agent_server_native_runtime/`：临时 RED/GREEN；可由用户手动整目录删除。
- `evals/system/incubating/agent_server_native_runtime/`：真实本地 Agent Server 的离线 deployment probe。

---

### Task 1: 建立 Agent Server 部署骨架与可验证配置

**Files:**
- Create: `langgraph.json`
- Create: `src/assistant_agent/agent_server/__init__.py`
- Create: `src/assistant_agent/agent_server/context.py`
- Create: `src/assistant_agent/agent_server/media_app.py`
- Create: `src/assistant_agent/agent_server/auth.py`
- Modify: `pyproject.toml`
- Create: `tests/tdd/agent_server_native_runtime/test_deployment_manifest.py`

**Interfaces:**
- Produces: `AgentServerRunContext`, `app`, `auth`, `assistant_graph` deployment symbol path。
- Consumes: current package install and mock provider configuration。

- [ ] **Step 1: 写 manifest RED 测试**

测试解析 `langgraph.json`，断言 `dependencies == ["."]`、graph 指向
`assistant_agent.agent_server.graph:assistant_graph`、custom app 指向
`assistant_agent.agent_server.media_app:app`、auth 指向
`assistant_agent.agent_server.auth:auth`，且 custom route auth 已启用。另断言
`AgentServerRunContext.model_json_schema()` 不含任意 Python service 类型。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_deployment_manifest.py
```

预期：FAIL，缺少 `langgraph.json` 和 `assistant_agent.agent_server`。

- [ ] **Step 3: 添加最小部署清单和 context**

`AgentServerRunContext` 使用 Pydantic 严格模型，仅包含：

```python
class AgentServerRunContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: str
    tenant_id: str
    assistant_mode: Literal["standard", "deep_research"] = "standard"
    entry_profile: str = "agent_server"
    media_capabilities: tuple[str, ...] = ()
```

custom app 初始只暴露 `/health/agent-server-adapter` 和尚未接线的 WebSocket route；auth 初始实现显式
mock/local developer principal，real mode 缺少受信 bearer/JWT verifier 时 fail closed。

- [ ] **Step 4: 将部署工具列为开发 optional dependency**

在 `pyproject.toml` 增加：

```toml
agent-server-dev = [
    "langgraph-cli[inmem]>=0.4.30,<0.5",
]
```

不在此步骤安装；先取得用户许可。

- [ ] **Step 5: 运行 GREEN 和 import smoke**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_deployment_manifest.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c \
  'from assistant_agent.agent_server.media_app import app; print(app.title)'
```

- [ ] **Step 6: 提交**

```bash
git add langgraph.json pyproject.toml src/assistant_agent/agent_server \
  tests/tdd/agent_server_native_runtime/test_deployment_manifest.py
git commit -m "feat(agent-server): add native deployment skeleton"
```

### Task 2: 分离可序列化 run context 与 worker 依赖

**Files:**
- Create: `src/assistant_agent/agent_server/services.py`
- Create: `src/assistant_agent/runtime/graph_services.py`
- Modify: `src/assistant_agent/runtime/graph_runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Create: `tests/tdd/agent_server_native_runtime/test_graph_service_binding.py`

**Interfaces:**
- Produces: `GraphExecutionServices`, `bind_graph_execution_services(...)`,
  `build_assistant_loop_graph(..., service_resolver=...)`。
- Consumes: `AgentServerRunContext` from Task 1 and existing governed services。

- [ ] **Step 1: 写 service-binding RED 测试**

构造两个并发 sentinel run，断言每个 node 只能取得本 run 的 `ToolExecutor/ChatAdapter/AgentState`，Graph
checkpoint/state/context JSON 不包含 service object；无绑定、身份不匹配和 introspection graph 执行均
fail closed。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_graph_service_binding.py
```

- [ ] **Step 3: 引入 `GraphExecutionServices`**

把现有 `GraphRuntimeContext` 中的 Python 对象移动到不可序列化、worker-owned 的 dataclass：

```python
@dataclass(frozen=True)
class GraphExecutionServices:
    tool_executor: ToolExecutor
    chat_adapter: ChatAdapter
    invocation_claim_store: GraphInvocationClaimStore
    context_service: ContextService | None
    trace_store: TraceStore | None
    # 其余现有 callbacks/services 原样迁移
```

`GraphRuntimeContext` 缩为受信 run facts；旧进程内 Runtime 通过显式 resolver 兼容，不能使用模块全局可变
singleton。

- [ ] **Step 4: 改造 node wrapper**

`bind_checkpointed_runtime_node` 在调用语义 node 前，通过 graph factory closure 的 resolver 将
`GraphExecutionServices + AssistantTurnState + AgentServerRunContext` hydrate 成 invocation-local
`AgentState`，节点返回后继续投影严格 `AssistantTurnState`。同一 graph topology 在 execute/read/schema
上下文保持一致。

- [ ] **Step 5: 迁移旧 Runtime 调用点**

更新现有进程内 Runtime，使其通过同一个 service resolver 运行原 Graph，保持迁移期离线测试能力；不得
新增另一个 assistant loop。

- [ ] **Step 6: 运行 GREEN 与现有 Graph 核心回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_graph_service_binding.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_memory_lifecycle.py
```

- [ ] **Step 7: 提交**

```bash
git add src/assistant_agent/runtime src/assistant_agent/agent_server/services.py \
  tests/tdd/agent_server_native_runtime/test_graph_service_binding.py
git commit -m "refactor(runtime): separate graph context from worker services"
```

### Task 3: 导出 Agent Server graph factory 并原生接入 Store

**Files:**
- Create: `src/assistant_agent/agent_server/graph.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_graph.py`
- Modify: `src/assistant_agent/memory/factory.py`
- Modify: `src/assistant_agent/memory/backends/langmem.py`
- Modify: `src/assistant_agent/memory/node_bundle.py`
- Create: `tests/tdd/agent_server_native_runtime/test_graph_factory.py`

**Interfaces:**
- Produces: `assistant_graph(runtime: ServerRuntime[AgentServerRunContext])` async context-manager factory。
- Consumes: Task 2 service resolver and `ServerRuntime.store/user/execution_runtime`。

- [ ] **Step 1: 写 factory RED 测试**

使用 `_ReadRuntime` 与 `_ExecutionRuntime` 形状的公开 protocol fake：断言四种 access context 返回完全相同
拓扑；execution context 创建并关闭一次 worker services；read/introspection 不初始化 Provider；Graph compile
不绑定 checkpointer；LangMem node 实际使用 server runtime Store。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_graph_factory.py
```

- [ ] **Step 3: 实现 factory owner**

factory 使用 `runtime.execution_runtime` 区分 execution 与 introspection，但两者调用同一个 topology builder。
execution 时验证 `runtime.ensure_user()` 与 context user/tenant 一致，创建 worker services；退出 async context
时按 Memory、Runtime service、Provider client 顺序关闭。

- [ ] **Step 4: 解除本地 Store/checkpointer 绑定**

`build_assistant_loop_graph` 新增明确 deployment compile mode：

```python
build_assistant_loop_graph(
    checkpointer=None,
    memory_bundle=bundle,
    bind_store=False,
    service_resolver=resolver,
)
```

LangMem manager 可在 factory 中使用 `ServerRuntime.store` 构造，但节点只从 `Runtime.store` 取得执行 Store；
Mem0/disabled 不要求 Store。

- [ ] **Step 5: 运行 GREEN 与 memory 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_graph_factory.py \
  tests/tdd/langgraph-memory-nodes \
  tests/core/integration/test_memory_lifecycle.py
```

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/agent_server/graph.py src/assistant_agent/runtime/assistant_loop_graph.py \
  src/assistant_agent/memory tests/tdd/agent_server_native_runtime/test_graph_factory.py
git commit -m "feat(agent-server): export native assistant graph factory"
```

### Task 4: 建立真实本地 Agent Server deployment probe

**Files:**
- Create: `evals/system/incubating/agent_server_native_runtime/README.md`
- Create: `evals/system/incubating/agent_server_native_runtime/checks_deployment.py`
- Modify: `.gitignore`
- Modify: `scripts/README.md`

**Interfaces:**
- Produces: 可重复执行的 `langgraph dev` + SDK probe，记录结构化 PASS/FAIL。
- Consumes: Tasks 1–3 deployment。

- [ ] **Step 1: 请求并安装开发依赖**

经用户明确许可后执行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install \
  -e '.[agent-server-dev]'
```

记录解析后的 `langgraph-cli`、`langgraph-api`、`langgraph-sdk` 版本，不写入凭证。

- [ ] **Step 2: 编写 deployment probe**

probe 启动独立临时目录/端口的 `langgraph dev --no-browser --no-reload`，显式设置
`LANGSMITH_TRACING=false` 和 mock provider；使用 SDK 验证 assistant schema、两个用户 thread 隔离、run
stream、Store、cancel、enqueue/interrupt 和 `Last-Event-ID` 恢复。开发 server 的本地持久目录不得进入 Git。

- [ ] **Step 3: 运行 probe**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/incubating/agent_server_native_runtime/checks_deployment.py
```

预期：所有结构化检查 PASS；若平台能力与规格不同，先回补规格和计划，不写自研替代 Runtime。

- [ ] **Step 4: 提交**

```bash
git add evals/system/incubating/agent_server_native_runtime .gitignore scripts/README.md
git commit -m "test(agent-server): add local deployment probe"
```

### Task 5: 提取媒体协议与 Agent Server SDK client

**Files:**
- Create: `src/assistant_agent/agent_server/client.py`
- Create: `src/assistant_agent/agent_server/media_protocol.py`
- Create: `src/assistant_agent/agent_server/media_session.py`
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Create: `tests/tdd/agent_server_native_runtime/test_media_native_mapping.py`

**Interfaces:**
- Produces: `AgentServerClient` protocol、`SdkAgentServerClient`、`MediaConnectionSession`、纯 frame mapper。
- Consumes: public `langgraph_sdk` and existing Media-Agent wire models。

- [ ] **Step 1: 写媒体映射 RED 测试**

复用现有媒体 fixture，断言 `assistantControl` 建立 thread correlation、`chat` 只创建一个原生 run、
PROCESSING/SUCCESS/FAIL frame shape 不变、`interrupt` 精确调用原生 cancel、ACK 只更新 delivery；断言 session
对象不存在 queue、runtime pool、Graph state 或 terminal inference 字段。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_media_native_mapping.py
```

- [ ] **Step 3: 实现公开 SDK client**

接口只暴露原生资源操作：

```python
class AgentServerClient(Protocol):
    async def create_thread(self, *, metadata: Mapping[str, object]) -> str: ...
    async def stream_run(self, *, thread_id: str, assistant_id: str,
                         input: Mapping[str, object], context: Mapping[str, object],
                         multitask_strategy: str) -> AsyncIterator[NativeRunEvent]: ...
    async def cancel_run(self, *, thread_id: str, run_id: str) -> None: ...
    async def join_thread(self, *, thread_id: str,
                          last_event_id: str | None) -> AsyncIterator[NativeRunEvent]: ...
```

实现使用 loopback `get_client(url=None)` 或经 Task 4 probe 证实的公开 deployment URL。

- [ ] **Step 4: 从旧入口提取纯协议代码**

迁移 envelope、message body、response builder、delivery ACK 和 media handler；旧 route 暂时调用同一纯模块，
保证迁移期无 wire drift。

- [ ] **Step 5: 运行 GREEN 与现有媒体 contract**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_media_native_mapping.py \
  tests/core/contract/test_gateway_contract.py
```

- [ ] **Step 6: 提交**

```bash
git add src/assistant_agent/agent_server src/assistant_agent/api/agent_service_websocket.py \
  tests/tdd/agent_server_native_runtime/test_media_native_mapping.py
git commit -m "refactor(media): map vendor protocol to Agent Server resources"
```

### Task 6: 将 `/agent-service/v1` 切换为 Agent Server custom route

**Files:**
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `src/assistant_agent/agent_server/media_session.py`
- Modify: `src/assistant_agent/agent_server/auth.py`
- Modify: `evals/system/incubating/agent_server_native_runtime/checks_deployment.py`
- Create: `tests/tdd/agent_server_native_runtime/test_media_custom_route.py`

**Interfaces:**
- Produces: Agent Server-owned `/agent-service/v1` WebSocket production route。
- Consumes: Task 5 SDK client and media protocol mapper。

- [ ] **Step 1: 写 custom route RED 测试**

通过 FastAPI WebSocket 测试 client + scripted `AgentServerClient` 验证握手、chat stream、并发后续 chat、
interrupt、disconnect、ACK 和 owner mismatch；任何输入都不得导入/构造 `GatewaySessionManager`、
`GatewayTurnFacade` 或 `AgentGraphRuntime`。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_media_custom_route.py
```

- [ ] **Step 3: 实现 custom route**

route 在通过 Agent Server custom auth 后创建 `MediaConnectionSession`；每个 chat 使用 `chatIndex + thread_id`
幂等登记一个 run consumer；原生 stream event 机械投影；媒体断开按当前协议 best-effort cancel 活动 run，但
不删除 thread/store；route 内部订阅断开使用 `last_event_id` join 恢复。

- [ ] **Step 4: 运行 GREEN 与真实 deployment 媒体 probe**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_media_custom_route.py
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/incubating/agent_server_native_runtime/checks_deployment.py --media
```

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/agent_server tests/tdd/agent_server_native_runtime/test_media_custom_route.py \
  evals/system/incubating/agent_server_native_runtime/checks_deployment.py
git commit -m "feat(agent-server): serve media protocol as custom route"
```

### Task 7: 切换所有生产入口并删除 Gateway Runtime

**Files:**
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/api/routes_agent.py`
- Modify: `scripts/run_server.py`
- Modify: `scripts/agent_cli.py`
- Delete: `src/assistant_agent/api/gateway_runtime.py`
- Delete: `src/assistant_agent/api/gateway_websocket.py`
- Delete: `src/assistant_agent/gateway/runtime_pool.py`
- Delete: `src/assistant_agent/gateway/runtime_adapter.py`
- Delete: `src/assistant_agent/gateway/runtime_backend.py`
- Delete: `src/assistant_agent/gateway/turn_facade.py`
- Delete or reduce after reference audit: `src/assistant_agent/gateway/session.py`, `queueing.py`, `bridge.py`, `delivery.py`
- Modify: relevant package exports and callers found by `rg`。
- Create: `tests/tdd/agent_server_native_runtime/test_no_parallel_runtime.py`

**Interfaces:**
- Produces: production commands that start/connect to Agent Server only；无并行 Gateway execution runtime。
- Consumes: Tasks 3 and 6 deployment。

- [ ] **Step 1: 写删除审计 RED 测试**

AST/manifest audit 断言生产 `src/assistant_agent/api`、custom route 和 scripts 不导入或调用
`AgentGraphRuntime.invoke/astream`、`GatewaySessionManager`、`GatewayRuntimePool`、
`GatewayRuntimeAdapter`、`GatewayTurnFacade`；`langgraph.json` 是唯一生产 graph serving entry。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_no_parallel_runtime.py
```

- [ ] **Step 3: 切换 HTTP/CLI/demo**

产品 HTTP 和 CLI 使用 `langgraph_sdk` 创建 thread/run 或显式标为 offline graph probe；默认 server 命令改为
启动 Agent Server deployment。保留的普通 FastAPI routes 若不属于 Agent Server custom app，则明确拆为
管理/回调服务且不能执行 Graph。

- [ ] **Step 4: 删除无引用 Gateway Runtime**

按 `rg` 引用图从叶子到根删除 runtime pool、backend adapter、queue/admission、turn facade、execution
session 和重复 stream/terminal。媒体 frame/schema 已迁移的旧 route 删除。若 `artifact_delivery` 等仍被
媒体 custom route 使用，移动到 `agent_server` 或中性 media package，不保留空壳 Gateway service。

- [ ] **Step 5: 运行删除审计和核心回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime/test_no_parallel_runtime.py \
  tests/tdd/agent_server_native_runtime \
  tests/core
```

- [ ] **Step 6: 提交**

```bash
git add -A src/assistant_agent scripts tests/tdd/agent_server_native_runtime
git commit -m "refactor(runtime): make Agent Server the sole production executor"
```

### Task 8: 更新权威文档、部署导航和最终验收

**Files:**
- Modify: `docs/authority.toml`
- Modify: `docs/gateway-architecture.md`（重写或删除并迁移 owner）
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `README.md`
- Modify: `scripts/README.md`
- Modify: `tests/core/INVARIANTS.md`
- Modify: existing core tests only for changed registered `GATE-001/IDENT-001/RUN-001` contracts。

**Interfaces:**
- Produces: 与最终代码一致的唯一 authority 和完整验收证据。
- Consumes: all prior tasks。

- [ ] **Step 1: 重定义受影响 core invariants**

`GATE-001` 从自研 Gateway 生命周期改为“媒体协议适配不拥有执行状态，thread/run/cancel/reconnect 由 Agent
Server 原生资源权威”；`IDENT-001` 明确 auth owner、thread、run、connection、delivery 分离；`RUN-001`
明确生产 run 由 Agent Server worker 执行。只更新这些 invariant 的既有负责文件。

- [ ] **Step 2: 同步 authority**

删除 Gateway session/queue/runtime owner 描述，建立 Agent Server deployment authority；媒体文档保持 wire
字段权威并改写内部映射。Memory 文档明确 Store 由 server 注入，Runtime event 文档删除本地 production
composition root。

- [ ] **Step 3: 运行文档校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
git diff --check
```

- [ ] **Step 4: 运行最终 mock/offline 核心与 deployment 验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent_server_native_runtime
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/incubating/agent_server_native_runtime/checks_deployment.py --all
```

- [ ] **Step 5: 完成源码审计**

```bash
rg -n "GatewayRuntimePool|GatewayRuntimeAdapter|GatewayTurnFacade|GatewaySessionManager" \
  src/assistant_agent scripts
rg -n "AgentGraphRuntime.*(invoke|astream)|get_agent_runtime\(" \
  src/assistant_agent/api src/assistant_agent/agent_server scripts
git status --short
```

预期：第一条只允许历史/明确离线测试引用或零结果；第二条生产路径零结果；工作区只含本任务提交与用户原有
未跟踪文件。

- [ ] **Step 6: 提交最终文档和 invariant 收口**

```bash
git add docs README.md scripts/README.md tests/core src/assistant_agent
git commit -m "docs: make Agent Server runtime architecture authoritative"
```

## 规格覆盖自审

- 原生 auth/thread/run/queue/cancel/reconnect/checkpoint/Store：Tasks 1、3、4、6。
- 媒体协议兼容和 ACK/交付独立：Tasks 5、6。
- Graph Tool/Provider/Memory 领域逻辑及 LangMem/Mem0：Tasks 2、3。
- Gateway Runtime 实际删除、非旁路保留：Task 7。
- 多用户、重联、取消、重复输入、重启证据：Tasks 4、6、8。
- authority、scripts、deploy、测试入口收口：Task 8。
- 无 `TBD/TODO/implement later`；跨任务类型和函数名一致。
