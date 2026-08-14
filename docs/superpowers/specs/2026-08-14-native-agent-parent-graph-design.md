# 原生 Agent 父图收敛设计

日期：2026-08-14  
状态：已确认，待实施计划

## 1. 背景与目标

当前项目的 Graph 调度、checkpoint、interrupt/resume、stream 和 Agent Server 生命周期已经使用
LangGraph，但主 Assistant、Workflow、产品事件、Tool 执行、Provider、Memory 和若干入口仍保留多层
自研 Runtime/facade。目标不是删除所有领域能力，而是删除与 LangGraph、LangChain 和 Agent Server
重复的运行时机制，让项目收敛为：

> 一个由 Agent Server 托管的父 `StateGraph`，包含快速与规划两条原生执行分支；项目只保留
> Provider、Media-Agent 和可插拔 Memory 三类必要适配。

本设计只覆盖生产主链。MCP server、A2A、automation、durable task、CLI/offline 等仍直接构造旧
`AgentGraphRuntime` 的外围入口，放到后续独立迁移阶段。

## 2. 设计原则

1. 生产只部署一个父 Graph、一个 Agent Server 生命周期和一套 thread/run/checkpoint/Store。
2. 快速或规划模式只能来自产品入口的结构化选择，不根据用户自然语言推断。
3. 快速模式使用 `create_agent` 提供的标准 Agent loop。
4. 规划模式使用显式 `StateGraph` 原生节点拓扑，但 worker 复用快速模式的同一个
   `create_agent` compiled subgraph。
5. Memory 在分流前召回、汇流后提交；同一 turn 只 recall/commit 一次。
6. 对外直接使用 Agent Server 原生 thread/run/stream/cancel/checkpoint/error 协议。
7. 不迁移旧 `AssistantTurnState` checkpoint；新 Graph 使用新的 assistant/graph 版本和新 thread。
8. 没有明确产品价值的兼容层不保留。
9. 不实施旧 `AgentState <-> AssistantTurnState` 的中间收敛阶段；增量 channel 原则直接应用于新父
   Graph 和 `PlanningState`。

## 3. 总体拓扑

```text
AssistantRootGraph
  START
    |
    v
  memory_recall
    |
    v
  route_execution_mode
    |------------------------------|
    v                              v
  fast_agent                  planning_graph
  (create_agent)                planner
                                   |
                                   v
                               Send(work items)
                                   |
                                   v
                         fast_agent workers (parallel)
                                   |
                                   v
                                  join
                                   |
                                   v
                                verifier
                                   |
                         pass -----+----- repair
                                   |
                                   v
                                finalize
    |------------------------------|
    v
  memory_commit
    |
    v
   END
```

`langgraph.json` 只注册 `AssistantRootGraph`。快速与规划分支共享 Agent Server 注入的 checkpointer、
Store、认证身份、thread、run、stream 和 LangSmith tracing。子图 namespace 用于区分分支和并行 worker，
不再建立产品侧平行 run/event 模型。

## 4. 输入、状态与输出

产品输入包含标准 messages 和显式模式：

```python
execution_mode: Literal["fast", "planning"] = "fast"
```

根状态基于 LangChain `AgentState`，只增加跨两条分支共享的最小字段：

```python
class AssistantRootState(AgentState):
    execution_mode: Literal["fast", "planning"]
    memory_context: tuple[str, ...]
    memory_status: Literal["ready", "empty", "degraded"]
```

根状态不再复制 Agent Server 已拥有的 run status、checkpoint identity、cancel phase、stream publish
状态或产品错误码。规划专用的 plan、work item、worker result、verification 和 repair counter 只存在于
`PlanningState` 子图，不污染快速模式状态。

两条分支都以标准 `AIMessage` 作为回答。父图不再执行 `compose_response`、`publish_response` 或产品事件
投影；最终 state 和 stream 直接遵循 LangGraph/LangChain 类型。

### 4.1 增量 channel 规则

新 Graph 的节点只返回自己负责的增量，不返回整份 state：

- `messages` 直接使用 `AgentState` 的 `add_messages` reducer；
- `execution_mode` 是普通 overwrite channel，只允许输入归一化边界写一次；
- `memory_context` 和 `memory_status` 由 `memory_recall` 写一次，分支节点只读，不使用 append reducer；
- `PlanningState.plan` 由 planner/repair 版本化 overwrite；
- `worker_results` 按稳定 `work_item_id` 合并，相同 ID 内容冲突时失败；
- `completed_work_item_ids` 使用集合并集语义，并在序列化时确定性排序；
- artifact 按稳定 artifact ID 合并，相同 ID 内容冲突时失败；
- `verification` 只由 verifier overwrite；
- `repair_count` 由单一 controller 写入新值，避免 replay 下增量重复累计；
- finalize 只追加新的 `AIMessage`，不再维护 `final_response` 副本。

Provider、Store、认证身份、客户端和运行配置只从 `Runtime` 获取，不进入 state。

## 5. 快速模式

快速模式由 `create_agent` 构建并编译为可复用子图：

- 模型是项目 Provider 提供的标准 `BaseChatModel`；
- Tool 是 LangChain Tool；
- MCP Tool 由官方 `langchain-mcp-adapters` 装配；
- messages、Tool call、ToolMessage、streaming 和 checkpoint 使用框架原生类型；
- HITL、model/tool call limit、retry、summarization 等优先使用官方 middleware；
- Memory context 通过标准 dynamic prompt/middleware 注入，不再由自研 ContextService 编译整套请求。

快速模式不保留另一套 assistant/tool/compose loop。

## 6. 规划模式

规划模式是显式 `StateGraph` 子图，保留无法由通用框架替代的业务语义：

- `planner` 使用 `BaseChatModel.with_structured_output()` 产生有界 DAG；
- `dispatch` 使用 `Send` 派发可并行 work item；
- 每个 `worker` 复用快速模式的同一个 `create_agent` 子图；
- `join` 使用 reducer 汇总结果；
- `verifier` 结构化判断通过、失败或需要修复；
- `repair` 只允许有界次数，并只重新派发需要返工的任务；
- `finalize` 汇总为标准 `AIMessage`。

planner、worker、verifier、DAG admission、约束绑定、join/repair 判据和最终交付语义仍是业务节点；
它们不会被误称为 LangGraph 自带能力。原生化的目标是删除外围 Runtime、持久化和产品协议重复层，
不是删除这些业务节点。

## 7. Provider 适配

Provider adapter 必须保留，但统一实现 LangChain `BaseChatModel`，而不是继续维护项目专属的
`ChatAdapter` 协议。adapter 负责：

- 同步和异步生成；
- `AIMessage` / `AIMessageChunk`；
- `bind_tools()` 与 Provider tool-call 格式；
- usage metadata；
- Provider 特有 citation、reasoning 和搜索来源 content block；
- mock 模式下的确定性实现。

计划删除 `ChatRequest`、`ChatResult`、`LLMEvent` 及 Provider 到产品事件的多级映射。真实 Provider
继续只允许在显式 real mode 和完整配置下运行。

## 8. Tool 与 MCP

主链不再把 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool` 作为统一硬边界。改用：

- LangChain Tool schema；
- `ToolNode` / `ToolRuntime` / `ToolMessage`；
- 官方 Tool retry、call limit 和 HITL middleware；
- 本地 Tool 静态装配；
- 官方 `MultiServerMCPClient` 和 MCP Tool adapter。

外部副作用权限与幂等由 Agent Server auth、Tool 自身及其下游业务 API 负责。Agent Runtime 不再维护
统一 operation ledger。实施时必须同步修改 `AGENTS.md` 和 Tool authority 中的旧硬边界。

## 9. Memory

Memory 使用固定节点模式：

```text
memory_recall -> fast/planning branch -> memory_commit
```

保留最小后端协议：

```python
class MemoryBackend(Protocol):
    async def recall(...) -> list[str]: ...
    async def commit(...) -> None: ...
```

支持：

- `langmem`：使用 Agent Server 注入的 `runtime.store` 和 LangMem manager；
- `mem0`：保留薄 Mem0 client/adapter；
- 第三方服务：实现同一最小 adapter；
- `disabled`：mock/local 默认后端。

同一 turn 在分流前只召回一次，结果作为冻结的 `memory_context` 供快速 Agent、planner、worker 和 verifier
读取；规划 worker 不重复召回。汇流后只提交一次最终用户/助手消息。

配置错误在 Graph factory 启动时失败。瞬时 recall 使用 LangGraph `RetryPolicy`；重试后仍失败则设置
`memory_status="degraded"` 并继续。commit 失败不覆盖已生成回答，通过 LangSmith span 记录。主链不再
维护本地 Memory commit ledger，优先使用 Agent Server run identity 和后端自身幂等能力。

## 10. Streaming、协议与媒体入口

公开客户端直接消费 Agent Server 原生：

- thread/run/cancel；
- `messages`、`updates`、`tasks`、`checkpoints`、`custom`；
- native run status/error；
- state history 和 time travel。

计划删除主链的 `GraphStreamPart`、`GraphStreamResult.parts`、`ProductEventProjector`、`AgentEvent`、
`RealtimeAgentEvent`、自研 run lifecycle 事件、Replay/Fork facade 和产品错误码映射。

`/agent-service/v1` 必须保留，但只负责 Media-Agent vendor frame、媒体引用、标准多模态 message 与
`langgraph_sdk` 之间的机械映射。它不得拥有 Graph、Runtime、queue、checkpoint 或 cancel 状态机。

## 11. Workflow 现状判断与迁移方式

当前 Workflow 可以称为“半原生”，更准确的表述是：

> 执行机制是 LangGraph 原生的，Workflow 产品模型与业务编排是自研的。

应保留的最小业务层包括 planner/worker/verifier 角色、DAG proposal/admission、约束绑定、必要的
join/repair 规则以及最终交付语义。它们迁入父 Graph 的 `planning_graph` 子图。

应删除或收缩：

- `WorkflowGraphHost` 中与 Agent Server thread/run 生命周期重复的部分；
- 与 checkpoint 重复保存的 Workflow 执行状态；
- 自研 resume/cancel/event facade；
- 可由 LangGraph task、interrupt、state history 表达的状态机代码；
- SQLite Workflow 状态、事件和进度中仅为复制 Graph 执行事实而存在的表与投影。

deliverable/artifact 若只是 Tool 结构化输出，优先使用 `ToolMessage.artifact`、Store 或标准消息 content
block；只有存在独立于 Graph 生命周期的真实业务交付资源时，才保留对应业务存储。

因此不执行“删除整个 Workflow 后重做”，而是把最小规划业务节点迁入统一父 Graph，并退休外围
Workflow Runtime/facade。

## 12. 错误与观测

- Agent Server run status 是终态事实源；
- Provider adapter 抛标准模型异常；
- Tool 错误由 ToolNode/middleware 转为 `ToolMessage` 或使 run 失败；
- 规划节点使用 `RetryPolicy` 和有界 repair edge；
- Memory 的降级语义只由两个 Memory 节点负责；
- Graph/Node/Model/Tool trace 使用 LangSmith 原生 tracing；
- 删除 canonical JSONL、重复 trace tree 和产品 lifecycle audit；
- 必须保留的合规日志由 Agent Server、业务 Tool 服务和基础设施日志承担。

## 13. 迁移策略

1. 新建父 Graph 和新的 assistant/graph 版本，不修改旧 checkpoint schema。
2. 旧 thread/checkpoint 只读归档；新请求创建新 thread。
3. 不继续实现旧 v4 checkpoint 字段迁移、`policy_digest` 演进或
   `AgentState <-> AssistantTurnState` hydrate/project 优化；现有未提交旧主链 diff 不作为新 Graph 前置依赖。
4. 先完成 Provider `BaseChatModel`、快速子图和固定 Memory 节点。
5. 再迁移规划业务节点，使 worker 复用快速子图。
6. 切换 `langgraph.json` 和 `/agent-service/v1` 到新父 Graph。
7. 验证生产主链后，直接退休旧 `AssistantTurnState` 主链、hydrate/project adapter、Runtime facade 和
   stream projection；届时再按文件归属处理现有旧主链未提交 diff，不提前回滚用户改动。
8. MCP server、A2A、automation、durable task、CLI/offline 入口随后分阶段迁移。

## 14. 验证要求

默认全部使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`：

- fast/planning 只能由结构化字段路由；
- fast Agent 的标准 message、Tool 和 stream 行为；
- planning 的 `Send` 并行、join、verifier、有限 repair；
- planning worker 确实复用同一个 fast Agent 子图；
- 两种模式每 turn 都只 recall/commit 一次；
- disabled、LangMem、Mem0 和第三方 adapter 契约；
- Agent Server thread/run/cancel/checkpoint/state history；
- `/agent-service/v1` 媒体兼容和原生 SDK 调用；
- 旧 thread 不进入新 Graph，新 thread 不读取旧 state；
- 新 Root/Planning 节点只返回增量更新，并验证 reducer 在并行、resume 和 replay 下保持确定性；
- 新主链不依赖 `assistant_loop_state_from_turn_state`、`assistant_turn_state_from_loop_state`、
  `bind_checkpointed_runtime_node` 或旧 `policy_digest` checkpoint 迁移；
- 主链不存在 `ProductEventProjector` 等已退休协议依赖。

真实 Provider、真实 Mem0/LangMem 或正式 system eval 仅在显式 real mode、完整未跟踪配置和 operator
确认下执行；最终报告必须说明调用范围与结果。

## 15. 非目标

- 本阶段不迁移外围入口；
- 不迁移旧 checkpoint/state；
- 不先优化再迁移旧 `AgentState <-> AssistantTurnState` 双状态体系；
- 不保留旧产品事件和错误码兼容层；
- 不重建 Agent Server 已提供的 queue、cancel、history 或 time-travel API；
- 不把 planner/worker/verifier 误删为“非原生代码”；
- 不要求 Mem0 伪装成 LangGraph `BaseStore`。
