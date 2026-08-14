# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-14

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant 父图、fast/planning 子图与原生 stream 的当前权威 |
| Owns | 父图拓扑、模式路由、标准 messages、create_agent、planning super-step、原生 stream/interrupt/checkpoint |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/` |
| 验证入口 | `docs/authority.toml` 中 `runtime-event-stream.verification` |
| 相邻 authority | Agent Server 见 [`gateway-architecture.md`](gateway-architecture.md)；Tool 见 [`tool-calling-architecture.md`](tool-calling-architecture.md) |

## 生产运行图

生产 Assistant 只有一个 `AssistantRootGraph`：

```text
START
  -> memory_recall
  -> execution_router
  -> fast_agent | planning_graph
  -> memory_commit
  -> END
```

`execution_mode` 是严格输入字段，只允许 `fast|planning`。路由函数只读取结构化字段，不从用户文本、关键词、
Tool 或 Memory 推断模式。父图不绑定 saver，由 LangGraph Agent Server 注入 checkpoint、thread、run、cancel、
resume 与 Store 资源。

fast 分支是 `create_agent` 编译出的 `AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、
messages channel 和官方 middleware，不维护项目自建 assistant/tool loop。

planning 分支是显式 `AssistantPlanningGraph`：planner 输出严格 `NativePlanProposal`，本地 admission 只校验节点
ID、依赖引用和 DAG 无环，`Send` 按依赖分 wave 并行派发 worker，join 后直接 finalize 为标准 `AIMessage`。
每个 worker 复用同一个 fast graph，不创建第二套 Runtime，也不重复父图 Memory 节点。当前不维护 verifier、
repair、revision、acceptance contract、deliverable binding 或 artifact provenance；只有真实产品需求出现后才增加。

## State 与恢复

生产 state channel、checkpoint 和 reducer 调度全部使用 LangGraph 原生能力。生产 state 以
`AgentState.messages` 的 `add_messages` reducer 为主；planning 并行结果使用
`Annotated[list[WorkerResult], operator.add]` 声明原生列表累积。父图只增加：

- `execution_mode`；
- 冻结的 `memory_context` 与 `memory_status`；
- planning 子图内部的 plan 与 worker result。

已完成节点直接从 worker result 推导，不保存平行 completed-ID channel，也没有项目自定义 result/artifact
reducer。Provider/Tool client、Memory backend、投递 Store、身份对象和 callback 不写入 checkpoint。旧
`AssistantTurnState` checkpoint 不迁移进新图；旧 assistant/thread 仅作只读历史或外围兼容，新图使用版本化
assistant ID `assistant-native-v1`。

Memory 重试、error handler 和失败后的 `Command(update=..., goto=...)` 均是 LangGraph 原生 node 扩展能力，
不是项目自研降级层。正常 recall 通过静态 edge 进入 `execution_router`；重试耗尽后 handler 写入显式
`memory_status=degraded` 并用 `Command` 回到同一 router。commit 失败也由 node error handler 用 `Command`
结束当前图，不覆盖已生成的答案。项目只声明“Memory 是辅助能力，因此失败仍继续”这一产品结果。

## 原生流与生命周期

生产消费者直接使用 Agent Server 的 messages/updates/values、thread/run、cancel、checkpoint、interrupt 与
resume 协议。模型 token 和 Tool 消息由 LangChain/LangGraph 原生 callback/stream 产生；项目不再投影
`GraphStreamPart`、`AgentEvent` 或产品 run 状态作为主链事实源。

`HumanInTheLoopMiddleware` 使用 state-aware `when` predicate：fast 模式自动放行，planning 模式对非 read
Tool 触发原生 interrupt；恢复使用 Agent Server/LangGraph `Command(resume=...)`。model/tool call limit、
只读 Tool retry 与 summarization 均由官方 middleware 承担。

## 已退役兼容边界

旧 assistant loop、Graph app、通用 Runtime facade、Workflow host 与旧 checkpoint/Memory node bundle 已删除。
`src/assistant_agent/runtime/` 只保留仍被 Tool、Provider、媒体、Context 或 durable task 使用的中立 DTO 与外围
治理模块；它不拥有 Graph 生命周期。主动投递的中立 DTO/Store 位于 `assistant_agent.proactive_delivery`。

评测侧只保留直接调用本生产父图的 `NativeGraphEvaluationTarget` 基元。旧 Runtime/Workflow/Release Review
runner 因绑定旧 state/evidence 合同而删除，后续行为评测必须基于标准 messages 与 native trace 重新建立。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/native-agent-parent-graph
```
