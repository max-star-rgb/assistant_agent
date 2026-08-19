# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-18

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant 父图、fast/planning 子图与原生 stream 的当前权威 |
| Owns | 父图拓扑、模式路由、标准 messages、create_agent、planning super-step、原生 stream/interrupt/checkpoint |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/` |
| 验证入口 | `docs/authority.toml` 中 `runtime-event-stream.verification` |
| 相邻 authority | Agent Server 见 [`agent-server-architecture.md`](agent-server-architecture.md)；Tool 见 [`tool-calling-architecture.md`](tool-calling-architecture.md) |

## 生产运行图

生产 Assistant 只有一个 `AssistantRootGraph`：

```text
AssistantRootGraph
  -> capture_trusted_runtime_facts
  -> memory_recall
  -> execution_router
       fast     -> AssistantFastAgent --------+
       planning -> AssistantPlanningGraph ----+
  -> refresh_memory_extraction
  -> END
```

`execution_mode` 是结构化输入字段，只允许 `fast|planning`；省略时按公开 input schema 默认使用 `fast`，以兼容
Studio 的标准 messages-only run。路由函数不从用户文本、关键词、Tool 或 Memory 推断模式。父图不绑定 saver，
由 LangGraph Agent Server 注入 checkpoint、thread、run、cancel、resume 与 Store 资源。

`capture_trusted_runtime_facts` 在 `memory_recall` 前采集带时区的当前时间与部署默认地点，写入结构化
`trusted_runtime_facts`。当前默认地点为“上海市青浦区华为练秋湖研发中心”，并显式标记
`source=deployment_default`、`is_fallback=true`；模型可见临时消息使用“用户默认地点”中文字段，并明确不得把该默认地点
表述为已观测到的用户物理位置。节点完成后快照随 checkpoint 冻结：从其后的 interrupt 恢复不会重新采集；
从更早 checkpoint replay 并重新执行该节点时允许刷新。这与 `memory_recall` 的原生节点恢复语义一致。

fast 与 planning 直接作为父图节点装配。fast 分支是 `create_agent` 编译出的 `AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、
messages channel 和官方 middleware，不维护项目自建 assistant/tool loop。

planning 分支是显式 `AssistantPlanningGraph`：planner 输出严格 `NativePlanProposal`，本地 admission 只校验节点
ID、依赖引用和 DAG 无环，`Send` 按依赖分 wave 并行派发 worker。调度器根据 `depends_on` 自动把直接上游
`WorkerResult` 组装为运行时 `dependency_results`，worker 将其作为明确的只读数据输入交给同一个 fast graph；
该字段不是 planner 输出 schema。全部节点完成后，finalize 用同一个模型根据原始请求和按 plan 排序的结果生成
标准 `AIMessage`，不机械拼接输出。planning 不创建第二套 Runtime，也不重复父图 Memory 节点。当前不维护
verifier、repair、revision、acceptance contract、deliverable binding 或 artifact provenance；只有真实产品需求
出现后才增加。

## State 与恢复

生产 state channel、checkpoint 和 reducer 调度全部使用 LangGraph 原生能力。生产 state 以
`AgentState.messages` 的 `add_messages` reducer 为主；planning 并行结果使用
`Annotated[list[WorkerResult], operator.add]` 声明原生列表累积。父图只增加：

- `execution_mode`；
- 冻结的 `memory_context` 与 `memory_status`；
- 冻结的 `trusted_runtime_facts`；

fast agent 子图才增加由成功 `load_skill` 标准 Tool 结果产生的 `active_skill_ids`
与窄 `skill_reference_grants`；这两个 channel 只在子图的 model→tool→model 循环中累积，不进入
父图节点、父图输出或独立 Memory Graph。planning 子图内部另外持有 plan、worker result，
并在 `Send` 派发时从直接依赖结果派生窄 `dependency_results` worker 输入。
planning 的 planner、worker 与 finalizer 都读取父图传入的同一份可信事实快照，不在子图内重新采集。

父图不投影或改写生成图片。`image_generation` 直接使用标准 `ToolMessage(content, artifact)`：模型下一次调用
只读取窄文本 `content`，程序消费者从 `artifact.images[]` 读取受管图片引用。最终 `AIMessage` 保持模型原始
回答，因此 Studio 当前只显示生成成功文本，不承诺图片预览；媒体 WebSocket 在入口适配层完成自己的 wire 投影。

已完成节点直接从 worker result 推导，不保存平行 completed-ID channel，也没有项目自定义 result/artifact
reducer。Provider/Tool client、Memory backend、投递 Store、身份对象和 callback 不写入 checkpoint。旧
`AssistantTurnState` checkpoint 不迁移进新图；旧 assistant/thread 仅作只读历史或外围兼容，新图使用版本化
assistant ID `assistant-native-v1`。

完整 Tool inventory 仍静态注册给 fast `create_agent` 的 `ToolNode`；每次 model call 的可见子集由原生 middleware
从上述 Skill 激活状态与受信 manifest 派生。该过滤不创建第二套 Tool runtime，也不改变 ToolNode 对已注册 Tool
的标准执行路径。

回答生成后，主图通过官方 Agent Server SDK 查询同 thread 的 pending runs，只对带
`assistant_agent_run_kind=memory_extraction` metadata 的旧 Memory run 执行 `cancel(..., action="rollback")`，
随后立即 enqueue 新 delayed Memory run；pending chat run 不受影响。Memory 重试、error handler 和失败后的 `Command(update=..., goto=...)` 均是 LangGraph 原生 node 扩展能力，
不是项目自研降级层。chat run 的 recall 重试耗尽后 handler 写入显式 `memory_status=degraded` 并跳到
`execution_router`；回答后的 refresh 失败只结束当前主图，不丢弃已经生成的回答。独立 Memory Graph 失败只影响后台 run。
项目只声明“Memory 是辅助能力，因此失败仍继续”这一产品结果。

## 原生流与生命周期

生产消费者直接使用 Agent Server 的 messages/updates/values、thread/run、cancel、checkpoint、interrupt 与
resume 协议。模型 token 和 Tool 消息由 LangChain/LangGraph 原生 callback/stream 产生；项目不再投影
`GraphStreamPart`、`AgentEvent` 或产品 run 状态作为主链事实源。

`HumanInTheLoopMiddleware` 使用 state-aware `when` predicate：fast 模式自动放行，planning 模式对非 read
Tool 触发原生 interrupt；恢复使用 Agent Server/LangGraph `Command(resume=...)`。model/tool call limit、
只读 Tool retry 与 summarization 均由官方 middleware 承担。

## 已退役兼容边界

旧 assistant loop、Graph app、通用 Runtime facade、Workflow host 与旧 checkpoint/Memory node bundle 已删除。
`src/assistant_agent/runtime/` 只保留仍被 Tool、Provider、媒体、Context 或 durable task 使用的中立 DTO；
Registry/Executor、产品事件投影与零消费者 Runtime DTO 已删除，它不拥有 Graph 生命周期。主动投递的中立
DTO/Store 位于 `assistant_agent.proactive_delivery`。

评测侧只保留直接调用本生产父图的 `NativeGraphEvaluationTarget` 基元。旧 Runtime/Workflow/Release Review
runner 因绑定旧 state/evidence 合同而删除，后续行为评测必须基于标准 messages 与 native trace 重新建立。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/native-agent-parent-graph
```
