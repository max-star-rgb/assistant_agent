# Architecture Layers

本文件说明仓库的稳定分层口径。它不是新的重构任务，而是给后续实现、评审和文档更新使用的边界说明。

## Summary

当前仓库继续采用五层物理目录：

```text
agent/                      Engine / Agent Core
services/                   Application Services
api/, mcp/                  Interfaces
tools/, providers/, memory/ Capability Adapters
schemas/, utils/, config.py Contracts & Platform
```

同时借鉴 Claude Code 式运行视角，把系统理解为：

```text
Engine Layer
Tool / Capability Layer
Service Layer
Safety & Governance Layer
```

这两个视角不冲突：五层目录回答“代码放哪里”，四层运行视角回答“请求经过哪些职责边界”。

## Physical Layers

### 1. Engine / Agent Core

目录：`agent/`

职责：

- Assistant / ReAct / Plan Mode loop。
- LangGraph runtime nodes and graph composition。
- Agent state transition、plan-mode hint compatibility、response handoff。
- 决定下一步做什么，但不直接调用 provider、store 或外部 API。

关键边界：

```text
assistant_node -> AssistantDecision
AssistantDecision -> ActionValidator -> ToolExecutor
```

`assistant_node` 可以选择 action，但不能绕过本地 validator / executor。

### 2. Application Services

目录：`services/`

职责：

- Assistant run service、session、run history、trace、event sink。
- Provider selection、provider diagnostics、provider readiness。
- Agent communication gateway、directory、local/outbound A2A transports、task/message routing service。
- Delegation context filtering, child-run budget metadata, tool-result reference pruning, and cross-agent artifact summaries.
- Pilot readiness checks, redacted control-plane summaries, and failure replay payload construction.
- 业务级服务封装，例如 memory audit、generated artifacts、video context。

服务层负责“运行时业务编排”，不直接变成 Agent 决策逻辑。

### 3. Interfaces

目录：`api/`, `mcp/`

职责：

- FastAPI HTTP API。
- WebSocket event stream。
- Inbound A2A JSON-RPC protocol adapter。
- MCP server packaging boundary。

接口层只负责协议转换、鉴权/用户边界、请求响应模型适配。它不应该绕过 `AgentGraphRuntime`、`ToolExecutor`、`MemoryManager` 或治理策略直接执行能力。

### 4. Capability Adapters

目录：`tools/`, `providers/`, `memory/`

职责：

- `tools/`：Agent 可调用能力边界，暴露 `ToolSpec`、input schema 和结构化 `ToolResult`。
- `delegate_to_agent` 等 agent communication 工具必须仍然是 `tools/` 能力，不允许 assistant node 直接调 communication service。
- `providers/`：真实或 mock provider 的具体适配实现。
- `memory/`：本地 memory backend、retrieval、store、profile/write policy 等基础能力。

工具层只暴露稳定能力合同。provider 是工具背后的实现细节，不应该泄露到 Agent 决策层。

### 5. Contracts & Platform

目录：`schemas/`, `utils/`, `config.py`, `runtime_profile.py`

职责：

- Pydantic API / tool / event / memory / provider contracts。
- Runtime profile and configuration defaults。
- Shared helpers。

公共数据结构优先放在 `schemas/`。业务对象优先从具体模块导入，不通过 `__init__.py` 聚合成隐式公共入口。

## Safety & Governance

Safety & Governance 是横切层，不必一开始独立成目录。当前主要由以下模块共同承担：

```text
ActionValidator
LoopGuard
ProviderCallBudget
ProviderExecutionPolicy
RuntimeProfile
MemoryWritePolicy
MemoryAuditService
AgentDirectory / AgentRoutingPolicy / AgentDelegationPolicy / AgentCommunicationService
DelegationContextBuilder / MemoryScopeFilter / ToolResultPruner
TraceStore redaction
API auth / user-session boundary
```

治理层负责：

- 工具调用前校验 tool name、tool input、required media、semantic input。
- 限制循环次数、重复失败和空决策。
- 控制 provider 调用预算、超时、重试和 fallback。
- 禁止真实 provider failure 静默降级成 mock success。
- 控制 memory 写入、敏感信息脱敏、用户隔离和审计删除。
- 控制 agent-to-agent 路由、目标启用状态、source/target allowlist、delegation depth、ping-pong loop、timeout metadata、child context filtering、transport 边界和 remote allowlist。
- 保证 trace、API errors、debug output 只暴露 redacted summary。

所有真实能力调用必须经过：

```text
AssistantDecision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> Tool
  -> Provider / Memory backend
  -> ToolObservation
```

禁止路径：

```text
assistant_node -> provider SDK
api/mcp -> provider SDK
api/mcp -> memory store
tool -> raw unredacted trace output
assistant_node -> remote agent/A2A client
real provider failure -> mock success
```

## Memory Boundary

记忆既有服务语义，也有适配器语义，不能一刀切归类。

服务层语义：

- `MemoryManager`
- memory audit service
- conversation history service
- context building
- write policy and lifecycle decisions

适配器层语义：

- `MemoryStore`
- `InMemoryStore`
- `JsonlMemoryStore`
- retrieval backend
- future SQLite / PostgreSQL / vector DB / external memory service adapter

Agent、assistant loop、API 和 memory tools 不应直接依赖底层 store。默认路径应是：

```text
Agent / Tool / API
  -> MemoryManager or MemoryAuditService
  -> MemoryStore / Retrieval backend
```

memory tool 只是 Agent/LLM 调用记忆服务的适配器，不是记忆服务本体。它只应负责 `ToolContext` 身份绑定、工具输入适配、调用 `MemoryManager`、包装 `ToolResult`；检索排序、写入策略、TTL、去重、用户画像、审计、snapshot 和 store 选择都应留在 `memory/` 或 `services/memory_*` 边界。

当前可以保留 `memory/` 目录，不需要为了分层口径立即迁移文件。后续如要移动，应通过独立任务完成，并保持 public contracts 和测试稳定。

## Where To Add Code

- 新增 assistant 决策、图节点、plan-mode 状态转换：放 `agent/`。
- 新增可被 Agent 调用的能力：先定义 `ToolSpec`、input schema、structured result，再放 `tools/`。
- 新增 agent-to-agent 委托能力：先放 `tools/`，通过 `ToolExecutor` 调用 `services/agent_communication.py`，默认不注册。
- 新增跨 agent context/budget/tool-result 过滤：放 `services/agent_delegation_context.py`，由 `AgentCommunicationService` 在 transport 前调用。
- 新增多 Agent 用户入口：放 `services/agent_gateway.py` 和 `api/` 路由，默认 `/agent/run` 不经过 gateway，显式 `/agents/run` 才启用。
- 新增 inbound A2A 协议入口：放 `api/routes_a2a.py` 和 `services/a2a_adapter.py`，只做协议转换并复用 `AgentGateway` / communication service。
- 新增具体外部模型或第三方 API：放 `providers/`，默认 mock/local，真实 provider 显式 opt-in。
- 新增运行服务、trace、session、audit、agent communication、provider 管理：放 `services/`。
- 新增 HTTP/WebSocket/MCP 入口：放 `api/` 或 `mcp/`，并复用 runtime/service/tool 边界。
- 新增公共请求、响应、事件、tool observation、provider spec：放 `schemas/`。
- 新增底层 memory backend：放 `memory/`，并通过 `MemoryManager` 暴露给上层。

## Change Checklist

实现或评审架构相关改动时，至少确认：

- Agent 决策没有直接调用 provider、store 或 HTTP client。
- API/MCP 入口没有绕过 runtime、service 或 tool executor。
- 新工具有结构化 input/output schema 和失败结果。
- 真实 provider 默认关闭，测试默认不调用真实外部服务。
- provider budget、timeout、retry、fallback 边界没有被绕过。
- memory 写入经过 `MemoryManager` / write policy。
- memory tool 没有承载检索、写入、画像、TTL、审计或直接 store 访问等服务逻辑。
- trace、error、debug output 没有泄露 secret、raw provider response、base64 或敏感路径。
- 修改行为时同步更新对应 docs/tasks，并补充测试覆盖。
