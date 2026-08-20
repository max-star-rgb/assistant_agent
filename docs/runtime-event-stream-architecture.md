# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-20

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant 父图、fast/planning 子图与原生 stream 的当前权威 |
| Owns | 父图拓扑、模式路由、标准 messages、create_agent、planning super-step、原生 stream/interrupt/checkpoint |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/` |
| 验证入口 | `docs/authority.toml` 中 `runtime-event-stream.verification` |
| 相邻 authority | Agent Server 见 [`agent-server-architecture.md`](agent-server-architecture.md)；Tool 见 [`tool-calling-architecture.md`](tool-calling-architecture.md)；视觉能力见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 生产运行图

生产 Assistant 只有一个 `AssistantRootGraph`：

```text
AssistantRootGraph
  -> capture_trusted_runtime_facts
  -> memory_recall
  -> execution_router
       fast     -> AssistantFastAgent --------+
       planning -> AssistantPlanningGraph ----+
       coding   -> AssistantCodingGraph ------+
  -> refresh_memory_extraction
  -> END
```

`execution_mode` 是结构化输入字段，只允许 `fast|planning|coding`；省略时按公开 input schema 默认使用 `fast`，以兼容
Studio 的标准 messages-only run。路由函数不从用户文本、关键词、Tool 或 Memory 推断模式。父图不绑定 saver，
由 LangGraph Agent Server 注入 checkpoint、thread、run、cancel、resume 与 Store 资源。

coding 分支是顺序 `AssistantCodingGraph`，只在结构化输入同时提供受信 allowlist 中的
`coding_repo_id` 时启用。它在 thread-scoped 临时 Git worktree 中执行 inspect/draft、确定性 patch validation、
digest-bound 原生 interrupt、受信 apply 和 apply 后的确定性 `run_validation`；模型不可见 apply、validation
进程、shell、delete、commit、merge 或 push。验证成功后才形成 applied terminal result；失败返回结构化
command evidence。formatter 只在 scratch 中生成增量 diff，该 diff 重新通过既有 validator 并带
`origin=formatter` 返回同一 digest-bound interrupt/apply 闭环；最多允许一轮 formatter patch，避免非幂等
循环。repository 显式启用 integration 时，验证成功后顺序执行 `create_commit -> prepare_merge ->
merge_approval -> apply_merge`。merge approval 是独立原生 interrupt，绑定 frozen source commit、expected
target HEAD 和 preview digest；apply 不调用模型，目标漂移或审批不匹配不会重新生成 preview。integration
关闭时保持阶段 2 applied terminal。coding 不复用 planning 并行 worker，所有 mutation 通过单一顺序节点完成。

repository 配置 Stage 4B1 dependency profile 时，`apply_patch` 后先由确定性 `plan_dependencies` 检查
approved changed paths 与严格 lockfile，只有 lockfile 变化才生成 dependency plan。独立
`coding_dependency_install` interrupt 绑定 profile、lockfile、egress policy 与 plan digest；approve resume
重新解析 worktree lockfile，任何漂移均 fail closed。Graph state 只保存 JSON-safe plan 与 approval status，
不保存 wheelhouse path、Docker network/container、proxy client 或文件句柄；获批 plan 只在同一次
`run_validation` 节点调用内 fetch、离线消费并清理。

`capture_trusted_runtime_facts` 在 `memory_recall` 前采集带时区的当前时间与部署默认地点，写入结构化
`trusted_runtime_facts`。当前默认地点为“上海市青浦区华为练秋湖研发中心”，并显式标记
`source=deployment_default`、`is_fallback=true`；模型可见临时消息使用“用户默认地点”中文字段，并明确不得把该默认地点
表述为已观测到的用户物理位置。节点完成后快照随 checkpoint 冻结：从其后的 interrupt 恢复不会重新采集；
从更早 checkpoint replay 并重新执行该节点时允许刷新。这与 `memory_recall` 的原生节点恢复语义一致。

fast 与 planning 直接作为父图节点装配。fast 分支是 `create_agent` 编译出的 `AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、
messages channel 和官方 middleware，不维护项目自建 assistant/tool loop。

planning 分支是显式 `AssistantPlanningGraph`：planner 输出严格 `NativePlanProposal`，本地 admission 根据
composition 注入的静态 Tool inventory 与同一份 Skill catalog 确定性校验节点 Tool、Planner 实际激活 Skill、
节点 Skill grant、真实 planner evidence 引用、deliverable producer/evidence 引用、节点上限、DAG 无环和依赖深度；
未知或未授权事实一律 fail closed，且不读取用户文本或内置领域规则。admission 失败时把有界错误码写入
planning state，并由原生 conditional edge 回到同一个 planner；最多允许两次 proposal revision，第三次失败以
有界 `NativePlanAdmissionError` 终止 run。revision 输入只增加既有 `PlannerEvidence` 的有界只读投影和错误码，
不注入旧 planner transcript、Tool schema 或原始异常；evidence、已激活 Skill 与 reference grant 沿用原生 state
reducer，只有候选计划被覆盖。成功 admission 清除错误并进入 scheduler。`Send` 按依赖分 wave 并行派发 worker。调度器根据 `depends_on` 自动把直接上游
`WorkerResult` 组装为运行时 `dependency_results`，worker 将其作为明确的只读数据输入交给同一个 fast graph；
该字段不是 planner 输出 schema。worker 只能继承节点 required Skill 与 Planner 实际快照的交集；admission 禁止节点把
`load_skill` 放入 worker Tool allowlist，worker phase 也确定性过滤该 Tool。显式允许 `load_skill_reference` 时，Tool
只能读取 scheduler 投影的既有 `skill_reference_grants`，不能扩大 Skill 或 reference grant。全部节点完成后，finalize 用同一个模型根据原始请求和按 plan 排序的结果生成
标准 `AIMessage`，不机械拼接输出。planning 不创建第二套 Runtime，也不重复父图 Memory 节点。当前不维护
verifier、repair ledger、acceptance contract 或 artifact provenance；deliverable 当前只做 producer/evidence
引用准入，不建立运行期 artifact binding。只有真实产品需求出现后才增加。

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
其 `plan_candidate`、`admission_error` 与 `revision_count` 只服务原生 revision edge；不建立 repair ledger、数据库、
队列、checkpoint adapter 或 shadow state。恢复后 scheduler 只从 checkpointed plan 与已累积 worker results 重算
下一 wave，不保存平行 ready/completed channel。
planning 的 planner、worker 与 finalizer 都读取父图传入的同一份可信事实快照，不在子图内重新采集。
`trusted_runtime_facts` 写入 checkpoint 时保存为 JSON-safe 字典（时间为 ISO 8601 字符串），模型调用边界再校验为
严格 Pydantic 值；它不依赖 checkpoint 对项目自定义类型的宽松 msgpack 反序列化。

父图不投影或改写生成图片。`image_generation` 直接使用标准 `ToolMessage(content, artifact)`：模型下一次调用
只读取窄文本 `content`，程序消费者从 `artifact.images[]` 读取受管图片引用。最终 `AIMessage` 保持模型原始
回答，因此 Studio 当前只显示生成成功文本，不承诺图片预览；媒体 WebSocket 在入口适配层完成自己的 wire 投影。

实时摄像头 chat 只通过最新标准 `HumanMessage` 的 `source=live_camera` video block 进入父图；其中可以携带
视觉模块生成的可信目标边界，但 JPEG、Provider client、task 和 lease 不进入 state。父图只通过标准 ToolNode
消费视觉结果，逐帧并发、等待和晚到结果语义见视觉 authority。

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

生产消费者直接使用 Agent Server 的 messages/updates/custom/values、thread/run、cancel、checkpoint、interrupt 与
resume 协议。原生 SDK/Studio 可显式选择所需 stream mode；媒体入口只订阅 messages/values，不消费
updates/custom。模型 token、Tool 消息和节点 state update 由 LangChain/LangGraph 原生 callback/stream 产生；项目不再投影
`GraphStreamPart`、`AgentEvent` 或产品 run 状态作为主链事实源。
父图中的 fast/planning 单元是子图，因此需要模型 token 的消费者必须显式启用原生 subgraph stream；媒体入口
仍只把标准 assistant 文本和受控兼容投影发送到 wire，不转发 planner、Tool 参数或 ToolMessage 正文。
Tool 执行通过官方 runtime stream writer 向 custom mode 发送 `tool_progress` 生命周期事件；只包含
`tool_name`、`tool_call_id` 与 `started|completed|failed`，不包含 Tool 参数或结果正文。由于 fast/planning
执行单元是父图子图，Agent Server SDK 消费者需要同时启用 subgraph stream 才能接收其中的 messages/custom。

`HumanInTheLoopMiddleware` 使用 state-aware `when` predicate：fast 模式自动放行，planning 的 planner 与 worker
阶段都对非 read Tool 在执行前触发原生 interrupt；恢复使用 Agent Server/LangGraph `Command(resume=...)`，
已完成的 Planner Tool 和 worker 不重放，scheduler 从 checkpoint state 重算后续 wave。model/tool call limit、
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
