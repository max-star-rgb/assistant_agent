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
  -> fast_agent | planning_graph
  -> delivery_dispatch（仅 pending_deliveries 非空）
  -> memory_commit
  -> END
```

`execution_mode` 是严格输入字段，只允许 `fast|planning`。路由函数只读取结构化字段，不从用户文本、关键词、
Tool 或 Memory 推断模式。父图不绑定 saver，由 LangGraph Agent Server 注入 checkpoint、thread、run、cancel、
resume 与 Store 资源。

fast 分支是 `create_agent` 编译出的 `AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、
messages channel 和官方 middleware，不维护项目自建 assistant/tool loop。

planning 分支是显式 `AssistantPlanningGraph`：planner 输出严格 `NativePlanProposal`，本地 admission 校验引用、
DAG 和 deliverable，`Send` 按依赖分 wave 并行派发 worker，join 后进入 verifier；局部 repair 以稳定 work item
ID 和递增 revision 重跑，最后写入标准 `AIMessage`。每个 worker 复用同一个 fast graph，不创建第二套 Runtime，
也不重复父图 Memory 节点。

## 原生主动投递节点

主动投递是父图中的普通领域节点，不是旧 Runtime facade。任意前序业务节点可以写入 checkpoint-safe
`pending_deliveries` tuple；fast/planning 汇流后仅在该 channel 非空时进入 `delivery_dispatch`。节点从
`Runtime.execution_info` 取得 native thread/run，从 `AssistantRunContext` 取得认证 user，并通过 composition
closure 中的 `ProactiveDeliveryStore` 幂等入队。Store/client 不进入 state、context 或 prompt。

节点按稳定 `message_id` 提供重试幂等；LangGraph retry、resume 或相同 task 重放不会生成第二行。入队完成后
清空 pending channel，记录 prompt-invisible `ProactiveDispatchState`，再进入唯一 `memory_commit`。缺少 Store
或 native thread/run 身份时 fail closed。媒体 presence、claim、ACK 与重连投递属于 custom route，不决定图路由。

## State 与恢复

生产 state 以 `AgentState.messages` 的 `add_messages` reducer 为主。父图只增加：

- `execution_mode`；
- 冻结的 `memory_context` 与 `memory_status`；
- checkpoint-safe `pending_deliveries` 与 `delivery_dispatch`；
- planning 子图内部的 plan、worker result、artifact、verification 和 repair count。

worker result/artifact 以稳定 ID 合并；同 revision 内容冲突 fail closed，更高 repair revision 才能替换旧结果。
Provider/Tool client、Memory backend、投递 Store、身份对象和 callback 不写入 checkpoint。旧
`AssistantTurnState` checkpoint 不迁移进新图；旧 assistant/thread 仅作只读历史或外围兼容，新图使用版本化
assistant ID `assistant-native-v1`。

## 原生流与生命周期

生产消费者直接使用 Agent Server 的 messages/updates/values、thread/run、cancel、checkpoint、interrupt 与
resume 协议。模型 token 和 Tool 消息由 LangChain/LangGraph 原生 callback/stream 产生；项目不再投影
`GraphStreamPart`、`AgentEvent` 或产品 run 状态作为主链事实源。

`HumanInTheLoopMiddleware` 只对显式标记为 write/dangerous 的受信 Tool 触发原生 interrupt；恢复使用
Agent Server/LangGraph `Command(resume=...)`。model/tool call limit、只读 Tool retry 与 summarization 均由
官方 middleware 承担。

## 兼容代码边界

`src/assistant_agent/runtime/` 与 `src/assistant_agent/workflows/` 仍保留旧外围消费者，后续逐项迁移；它们不被
生产 `agent_server/` 或 `native_agent/` 导入，也不是 `langgraph.json` 的生产 graph。主动投递的中立 DTO/Store
位于 `assistant_agent.proactive_delivery`，旧模块仅作 import compatibility，不拥有生产执行。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/native-agent-parent-graph \
  tests/tdd/native-proactive-delivery/test_native_dispatch.py
```
