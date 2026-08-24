# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-21

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant 父图、fast/planning 子图与原生 stream 的当前权威 |
| Owns | 父图拓扑、模式路由、标准 messages、create_agent、planning/coding super-step、原生 stream/interrupt/checkpoint |
| Does not own | Agent Server HTTP 生命周期、Tool schema、Memory 后端、媒体 wire、Provider 凭据 |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/`、`src/assistant_agent/coding/analysis.py`、`src/assistant_agent/coding/models.py` |
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
Studio 的标准 messages-only run。同一 `assistant-native-v2` graph 还可由 Agent Server 中固定的
`assistant-native-v2-planning` assistant 资源提供 `assistant_execution_mode=planning` context preset；
`execution_router` 先把该 preset 规范化进 state，因此选择该 assistant 时即使 input schema 补入 fast 也固定进入
planning。普通 assistant 没有 preset，仍完全遵循结构化 input。路由函数不从用户文本、关键词、Tool 或 Memory
推断模式。父图不绑定 saver，
由 LangGraph Agent Server 注入 checkpoint、thread、run、cancel、resume 与 Store 资源。

coding 分支是显式 `AssistantCodingGraph`，只在结构化输入同时提供受信 allowlist 中的
`coding_repo_id` 时启用。它在 thread-scoped 临时 Git worktree 中执行 inspect/draft、确定性 patch validation、
digest-bound 原生 interrupt、受信 apply 和 apply 后的确定性 `run_validation`；模型不可见 apply、validation
进程、shell、delete、commit、merge 或 push。验证成功后才形成 applied terminal result；失败返回结构化
command evidence。formatter 只在 scratch 中生成增量 diff，该 diff 重新通过既有 validator 并带
`origin=formatter` 返回同一 digest-bound interrupt/apply 闭环；最多允许一轮 formatter patch，避免非幂等
循环。repository 显式启用 integration 时，验证成功后顺序执行 `create_commit -> prepare_merge ->
merge_approval -> apply_merge`。merge approval 是独立原生 interrupt，绑定 frozen source commit、expected
target HEAD 和 preview digest；apply 不调用模型，目标漂移或审批不匹配不会重新生成 preview。integration
关闭时保持阶段 2 applied terminal。coding 不复用 planning 并行 worker，所有 mutation 通过单一顺序节点完成。

repository 的 `parallel_analysis_enabled` 静态配置默认关闭；关闭时从 `resolve_workspace` 直接进入既有
`inspect_and_draft`。显式启用后，首次 draft 前由 `prepare_analysis` 冻结同一 identity/thread/workspace 绑定的
只读 snapshot，并通过原生 `Send` 在一个 super-step 中把三个固定 task 派发给共享只读 analysis agent；worker
只暴露 snapshot-bound list/search/read/status/diff Tool，不提供 shell、network、proposal、command、credential、
artifact 或 integration Tool；每个 worker state 强制 `provider_search_profile=none`，从模型调用边界禁用
Provider-native search。`join_analysis` 确定性校验、去重、排序并裁剪有界 `CodingAnalysisResult`，随后只进入唯一
`inspect_and_draft -> validate_proposal` 顺序入口。单个普通分析失败形成
`partial`，全部普通失败形成 `unavailable`，两者都只降级 advisory evidence，不绕过后续 validator、HITL、gate、
validation 或 integration。身份、权限、snapshot 隔离与 contract 错误仍 fail closed。

analysis worker 的临时 task instruction、AI/Tool transcript 和原始 structured response 不写入主 `messages`；
primary inspect 只在首次 draft 获得一次有界临时 context，并仍须用实时 workspace read Tool 自行确认。
analysis 相关 checkpoint channel 保存 opaque snapshot contract、固定 task、有界规范化 result、
`pending|completed|partial|unavailable` status、`active|released|cleanup_pending` release status 与布尔
`analysis_context_consumed`，不保存 snapshot 宿主路径、文件句柄、Git process、backend client 或 worker transcript。
pending analysis 恢复仍严格校验 active 物理 snapshot、身份、workspace/base 与 digest；join 且临时 context 已消费后，
approval/repair 等后续恢复只校验 checkpoint 中的有界 contract、固定 task inventory、result digest、workspace 与 base，
不再把已释放 snapshot 的物理目录或 TTL 当作 mutation gate。
repair 回边从
`prepare_repair -> consume_repair_budget -> inspect_and_draft` 恢复，formatter/patch/dependency/credential/artifact/
merge approval resume 也不重新进入分析；所有写入、命令、凭据、artifact 与 Git integration 继续位于唯一顺序
mutation lane。

`run_validation` 遇到一个确定性 `test|lint|build` 命令的普通非零退出，且错误码为
`verification_command_failed` 时，才由本地策略选中 eligible failure，沿原生
`run_validation -> prepare_repair -> inspect_and_draft` 回边进入最多两轮 repair。模型不能提交、
改写或放宽 eligibility；runner、workspace、sandbox、resource、timeout、cleanup 等基础设施错误
直接结束，不进入 repair loop。当轮有界 stdout/stderr 和 digest 只在 repair 模型调用边界组装为
临时 evidence context，不追加到原始 `messages` channel。

每轮 repair 只能提交一个增量 patch；候选累计 diff 在不改写 worktree 的临时 index 中预览，并绑定
patch、当前 workspace diff 与候选累计 diff 的 digest 进入独立 patch HITL。resume 后重算并校验全部
digest；重复 patch、累计 diff 不变或轮次停滞以 `coding_repair_no_progress` 终止。获批 patch 仍沿同一
mutation lane 执行 `apply_patch`，清空旧 approval 及下游 gate 状态，然后重新经过 dependency、
credential、artifact 与完整 validation gates。只有最终验证通过才能进入 integration。repair 不创建
第二套 Runtime、Graph 或 run，也不引入并行 mutation lane。

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
messages channel 和官方 middleware，不维护项目自建 assistant/tool loop。planning 的 planner、worker 和 finalizer
也复用这一个 compiled fast graph：分别通过 `agent_phase="planner|worker|finalizer"` 选择 model-call 投影，
不创建独立 Agent 或模型调用链。planner phase 的可执行 Tool projection 固定收窄到 `load_skill` 与
`load_skill_reference`，同时关闭 Provider-native search；默认可见和 Skill 治理的业务 Tool 都不能由 planner
执行。它使用 planner system role 与严格 `NativePlanProposal` structured response，把业务工作委派给 worker。

planning 分支是显式 `AssistantPlanningGraph`：production composition 从 `max_tool_iterations` 构造唯一
`PlanningBudgetPolicy`，并把同一实例注入共享 fast agent 与 planning graph；该 policy 是受信进程配置，不进入
公开 Graph input。planner 可在共享的 model→ToolNode→model loop 中调用上述 Skill 控制 Tool；成功
`load_skill` 产生的 active Skill/reference grant 随 planning state reducer 保存。渐进式 exposure 在加载 Skill 后
仍计算受信业务能力，但 planner phase 只把它转换为不可执行、无参数 schema 的有界 worker capability catalog；
未加载 Skill 时被治理的能力不出现，默认业务能力可列入 catalog，但都只能写进 worker 节点 allowlist。planner
随后输出严格 `NativePlanProposal`，其中 deliverable 必须引用 producer node 或既有 planner evidence。本地 admission 根据
composition 注入的静态 Tool inventory 与同一份 Skill catalog 确定性校验节点 Tool、Planner 实际激活 Skill、
节点 Skill grant、真实 planner evidence 引用、deliverable producer/evidence 引用、节点上限、DAG 无环和依赖深度；
校验还读取 checkpointed global usage、未 reconciliation 的 wave reservation 与同一 policy：每个候选 worker
至少预留一次 model call 和一次 node attempt，并始终为 finalizer 再保留一次 model call 和一次 node attempt；
最小需求超过当前剩余 graph budget 时以稳定 `insufficient_graph_budget` 拒绝，Tool 最小需求为零。该计算只按
checkpoint state 和稳定 ID 顺序完成，不依赖并行结果 arrival 顺序。
未知或未授权事实一律 fail closed，且不读取用户文本或内置领域规则。admission 失败时把有界错误码写入
planning state，并由原生 conditional edge 回到同一个 planner；最多允许两次 proposal revision，第三次失败以
有界 `NativePlanAdmissionError` 终止 run。`PlannerEvidence` channel 仅为既有 checkpoint/revision 契约保留；
新 planner phase 不执行业务 Tool，因此不会新增业务 evidence。revision 输入只增加既有 evidence 的只读投影和错误码；
该投影以 `trust=tool-output` 标记，JSON 在嵌入标签前转义 `<>&`，并以最终渲染字符数硬限制为 48,000。
预算先保留稳定顺序的 evidence ID、Tool 名与状态，再有界保留 content/artifact ref，不输出半个 JSON。
它不注入旧 planner transcript、Tool schema 或原始异常；evidence、已激活 Skill 与 reference grant 沿用原生 state
reducer，只有候选计划被覆盖。generation 0 可在首次成功 admission 前通过 revision 修正 scope；首次成功
admission 冻结该计划实际声明或使用的 Skill ID、reference ID 与 Tool name 为 checkpointed strict authorization
envelope。后续 generation 只能使用它的子集，任一新增 scope 以稳定 `authorization_expansion` fail closed；planner
prompt/context 与 scheduler projection 同时收窄到 envelope。replan 禁止再次调用 `load_skill`，只有 frozen
reference grant 非空时才保留 `load_skill_reference`；其 worker capability catalog 也只列 envelope 内 Tool。
checkpoint 不保存原始 Tool 参数或结果。
成功 admission 清除错误并进入 scheduler。`Send` 按依赖分 wave 并行派发 worker。调度器根据 `depends_on` 自动把直接上游
`WorkerResult` 组装为运行时 `dependency_results`，worker 将其作为明确的只读数据输入交给同一个 fast graph；
该字段不是 planner 输出 schema。其 generation-aware 原生拓扑是
`planner -> assess_planner -> admit_plan|planner|prepare_replan|controlled_finalize`，其中
`assess_planner -> planner` 承担同一 generation 内有界的 operational retry；worker 路径为
`scheduler -> reserve_wave_budget -> Send(worker) -> join -> reconcile_wave_budget -> assess_workers`。
planner 和 worker 都把预期结果收敛为严格 `PlannerOutcome` / `WorkerOutcome`；明确可重试的超时、连接或临时
HTTP 执行失败先在同一 generation 内有界 retry，业务不足、phase budget 耗尽或 operational retry 耗尽后经
`assess_workers -> prepare_replan -> planner` 进入下一 generation。`prepare_replan` 先冻结已成功
`WorkerResult`，新计划只能替换失败工作并显式引用 frozen result；checkpointed 单调 replacement claim ledger
允许相同 claim replay 幂等，但拒绝 later generation 用不同 replacement 再次 claim 同一 historical node。
scheduler 从 checkpointed generation、plan、outcome 与 frozen result 重算 ready wave，已成功 worker不重放。
`GraphBubbleUp` / interrupt / cancel、准入、鉴权与程序契约错误不进入该预期失败边界，仍保持 LangGraph 原生
传播。planner、worker、finalizer 在 operational 分类与 sanitizer 前使用同一 cause/context chain 检查并原样
抛出 control-flow；checkpoint 不写入外层 wrapper marker。checkpoint 中出现没有 live exception 的
`RecoveryDecision(action="propagate")` 属于契约错误，明确 fail closed，不进入 controlled finalizer。零节点 plan 不派发 worker，沿
`scheduler -> reserve_wave_budget -> finalize` 完成；`reserve_wave_budget` 的条件路线还会依据 ready wave 与预算
进入 worker 或 `controlled_finalize`。worker 只能继承节点 required Skill 与
Planner 实际快照的交集；admission 禁止节点把
`load_skill` 放入 worker Tool allowlist，worker phase 也确定性过滤该 Tool。显式允许 `load_skill_reference` 时，Tool
只能读取 scheduler 投影的既有 `skill_reference_grants`，不能扩大 Skill 或 reference grant。全部节点完成后，
finalizer 仍调用共享 `AssistantFastAgent`，但 `agent_phase="finalizer"` 确定性清空 Tool 与 structured response，
根据原始请求、deliverables、planner evidence 和按 plan 排序的 worker results 返回标准 `AIMessage`，不机械拼接输出。
finalizer 的 operational failure 只有有预算时才有界重试；模型终态不合约或 planner/worker/global recovery
耗尽时进入确定性 `controlled_finalize`，只用稳定 failure code、deliverable ID、generation 与冻结结果生成
标准 `AIMessage`，不暴露原始异常或另建产品终态。
worker 的直接依赖与所引用 PlannerEvidence 使用最终转义字符计数的 48,000 字符单消息预算；
finalizer 的最新请求、deliverables、全部 PlannerEvidence 和按 plan 排序的 WorkerResult 使用 96,000 字符单消息预算。
两者先保留 ID、状态、来源与 artifact ref，再确定性公平分配 content 字符并在 JSON 中标记裁剪；
始终生成完整 JSON，不依赖 `SummarizationMiddleware` 压缩巨型单条 `HumanMessage`。兼容 checkpoint 中既有的
Planner Tool artifact 在读取时按深度 8、最多 512 个 mapping/sequence item 增量遍历，检测循环并过滤 raw/unsafe key；
`structured_content` 最终 JSON 不超过 50,000 bytes，超限或未知对象只产生 JSON-safe truncation marker，
在边界内遇到的受信 `output_ref` / `artifact_ref` 仍单独保留。
planning 不创建第二套 Runtime，也不重复父图 Memory 节点。当前不维护
verifier、repair ledger、acceptance contract 或 artifact provenance；deliverable 当前只做 producer/evidence
引用准入，不建立运行期 artifact binding。只有真实产品需求出现后才增加。

## State 与恢复

生产 state channel、checkpoint 和 reducer 调度全部使用 LangGraph 原生能力。生产 state 以
`AgentState.messages` 的 `add_messages` reducer 为主；planning 并行 attempt 使用 canonical execution ID
映射累积 typed `WorkerOutcome`，成功 `WorkerResult` 再按 work item ID 单调冻结。父图只增加：

- `execution_mode`；
- 冻结的 `memory_context` 与 `memory_status`；
- 冻结的 `trusted_runtime_facts`；

fast agent 子图才增加由成功 `load_skill` 标准 Tool 结果产生的 `active_skill_ids`
与窄 `skill_reference_grants`；这两个 channel 只在子图的 model→tool→model 循环中累积，不进入
父图节点、父图输出或独立 Memory Graph。planning 子图内部另外持有 plan、worker result，
并在 `Send` 派发时从直接依赖结果派生窄 `dependency_results` worker 输入。
其 `plan_candidate`、`admission_error` 与 `revision_count` 只服务原生 revision edge；recovery 另外保存有界的
`plan_generation`、typed planner/worker outcome、budget usage、wave reservation、recovery decision/history/context、
首次成功 admission 冻结的 strict authorization envelope、单调 replacement claim ledger、
`frozen_worker_results` 与 superseded ID，不建立数据库、队列、checkpoint adapter 或 shadow state。恢复后
scheduler 只从这些 checkpointed typed channels 重算下一 wave，不保存平行 ready/completed channel；冻结成功
结果的 reducer 拒绝同 ID 冲突，replan/resume 都不会再次派发它。`WorkerResult.sources` 在 JsonPlus/msgpack 的 JSON list 边界
规范化回 tuple，避免 strict Pydantic checkpoint 恢复退化为未校验构造。
planning 的 planner、worker 与 finalizer 都读取父图传入的同一份可信事实快照，不在子图内重新采集。
`trusted_runtime_facts` 写入 checkpoint 时保存为 JSON-safe 字典（时间为 ISO 8601 字符串），模型调用边界再校验为
严格 Pydantic 值；它不依赖 checkpoint 对项目自定义类型的宽松 msgpack 反序列化。

coding 子图内部的 analysis channel 是 opaque `CodingAnalysisSnapshot`、三个固定 task，以及通过 state schema
显式声明的确定性 task-ID replacement reducer 所维护的有界 `CodingAnalysisResult`；该 reducer 在 checkpoint replay
时以同 task 新结果替换旧结果，不使用并行 append reducer。其余 channel 包括 `pending|completed|partial|unavailable` 状态、
`active|released|cleanup_pending` snapshot release 状态和 `analysis_context_consumed`。pending checkpoint 恢复时只
派发尚未完成的 task；已 join、repair active 或任一 approval resume 都从既有 checkpoint 继续，不创建新 snapshot
或重跑已完成分析。

父图不投影或改写生成图片。`image_generation` 直接使用标准 `ToolMessage(content, artifact)`：模型下一次调用
只读取窄文本 `content`，程序消费者从 `artifact.images[]` 读取受管图片引用。最终 `AIMessage` 保持模型原始
回答，因此 Studio 当前只显示生成成功文本，不承诺图片预览；媒体 WebSocket 在入口适配层完成自己的 wire 投影。

实时摄像头 chat 只通过最新标准 `HumanMessage` 的 `source=live_camera` video block 进入父图；其中可以携带
视觉模块生成的可信目标边界，但 JPEG、Provider client、task 和 lease 不进入 state。父图只通过标准 ToolNode
消费视觉结果，逐帧并发、等待和晚到结果语义见视觉 authority。

已完成节点直接从 frozen result 与当代 typed outcome 推导，不保存平行 completed-ID channel，也不维护
项目自建 result/artifact runtime。Provider/Tool client、Memory backend、投递 Store、身份对象和 callback
不写入 checkpoint。旧
`AssistantTurnState` checkpoint 不迁移进新图；旧 assistant/thread 仅作只读历史或外围兼容，新图使用版本化
assistant ID `assistant-native-v2`。项目创建的可运行 thread 以 metadata `assistant_graph_id` 绑定具体 graph；
普通 v2 run/resume/stream 在创建 run 前精确验证该值。`assistant-native-v1` 或缺失 identity 的 unknown thread
及其 checkpoint 同样只读，不能进入 v2 run/resume/replay；该 graph ID 升级没有自动 migration。历史 state、
thread 和 stream inspection 不因此被禁止，部署阶段仍可 drain/cancel legacy run；校验函数接收调用方期望的
graph ID，不阻止 Memory 等独立 Graph 使用自己的 thread 与版本身份。

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
resume 协议。媒体入口只订阅 messages/values，不消费
updates/custom。模型 token、Tool 消息和节点 state update 由 LangChain/LangGraph 原生 callback/stream 产生；项目不再投影
`GraphStreamPart`、`AgentEvent` 或产品 run 状态作为主链事实源。
父图中的 fast/planning 单元是子图，因此需要模型 token 的消费者必须显式启用原生 subgraph stream；媒体入口
仍只把标准 assistant 文本和受控兼容投影发送到 wire，不转发 planner、Tool 参数或 ToolMessage 正文。
Tool 执行通过官方 runtime stream writer 向 custom mode 发送 `tool_progress` 生命周期事件；只包含
`tool_name`、`tool_call_id` 与 `started|completed|failed`，不包含 Tool 参数或结果正文。由于 fast/planning
执行单元是父图子图，需要完整协议的 Agent Server SDK/API 消费者分别请求
`stream_mode=["messages", "updates", "custom"]` 并设置 `stream_subgraphs=True`；进程内调用
`graph.astream(...)` 时使用相同的 `stream_mode`，并设置 `subgraphs=True`。`stream_mode` 选择事件类型，subgraph
开关决定嵌套 namespace 是否可见，两者互不替代。
planning recovery 节点同样只通过原生 custom mode 发出 `recovery_transition`，字段固定为 `from`、`to`、
`reason_code` 与 `plan_generation`；节点 state 变化仍由 updates/values 提供，终态仍由 messages 提供。
Graph Studio 用于查看 graph/subgraph 执行、trace 与调试信息；其具体 UI 能力随 Studio 版本演进，不作为任意
custom payload 的通用渲染承诺。需要完整核验恢复事件和嵌套 namespace 时，应使用上述 SDK/API 订阅。媒体
custom route 不订阅或重解释该事件，也不建立 shadow event bus。

`HumanInTheLoopMiddleware` 使用 state-aware `when` predicate：fast 模式自动放行；planning worker 对非 read
Tool 在执行前触发原生 interrupt，planner 只有 Skill 加载控制 Tool。恢复使用 Agent Server/LangGraph
`Command(resume=...)`，已完成的 Skill 加载、冻结 worker 和已结算 wave 不重放，scheduler 从 checkpoint state
重算后续 wave。
phase model/tool call limit 由 `PhaseBudgetMiddleware` 承担，planning graph 再以同一 policy 结算 global
model/tool/node/replan budget；只读 Tool retry 与 summarization 继续使用官方 middleware。

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
  tests/tdd/native-high-agency-planner
```
