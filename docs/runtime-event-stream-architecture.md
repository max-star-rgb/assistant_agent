# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-24

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
Studio 的标准 messages-only run。同一 `assistant-native-v3` graph 还可由 Agent Server 中固定的
`assistant-native-v3-planning` assistant 资源提供 `assistant_execution_mode=planning` context preset；
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

final coding review 由 `CodingRepositoryConfig.code_review_enabled` 显式启用并默认关闭；关闭时，validation 成功后
保持既有 applied terminal 或 integration 分支，不创建 review snapshot、不运行 reviewer，也不新增 approval。
启用时，最终一轮 validation 全部通过后依次进入 `prepare_review_snapshot -> run_code_review ->
coding_review_decision`：父图复用 validation 执行前冻结且成功后重新核验的只读 final snapshot，review 子图用原生 `Send` 并行派发三个
固定 reviewer，再确定性 join 为有界 canonical report。reviewer 只使用 snapshot-bound list/search/read/status/diff
Tool，固定 `provider_search_profile=none`，不拥有 proposal、apply、command、dependency、credential、artifact、
commit、merge、network 或其他 mutation 能力。普通 reviewer/capability 失败形成 `unavailable` report；它不自动
修复、不回到 draft/repair，也不绕过独立 user decision。cancel、permission、Graph control-flow、identity、schema、
snapshot 与 digest 错误仍 fail closed 或原样传播。

review decision 是 patch approval 和 merge approval 之外的独立 digest-bound 原生 interrupt。父 checkpoint、
`CodingReviewInput`、每个 signed result、canonical report 和 decision payload 共同绑定 generation、workspace、base、
snapshot ref、materialization schema version、tree/diff digest、snapshot 创建/过期时间、patch digest、最终 validation
evidence digest 与 report digest。pending review 只接受 active `immutable_manifest_v2` 物理 snapshot；completed report
resume 可在原 snapshot 已自然过期或物理资源已清理后，针对当前 workspace 重新创建 fresh v2 snapshot 并比较冻结内容
身份。只有缺少新 version channel/字段且 canonical digest 仍符合历史编码的真正 completed v1 checkpoint 可以通过
`legacy_v1 -> immutable_manifest_v2` fresh rebind；pending v1、v2 downgrade、任一 version mismatch 或当前 v2
manifest/schema mismatch 都 fail closed。review reject 形成 rejected terminal；approve 在 integration 关闭时形成 applied
terminal，在 integration 显式开启时才顺序进入既有 controlled commit/merge lane。review snapshot 只受既有 snapshot
owner、lease、TTL 与 snapshot-only reaper 管理；validation failure/workspace-change、review-off terminal 与 commit comparison
退出时确定性、幂等 release，不再需要的 lease 不留到 TTL，仍由下一 checkpoint 的 review/commit 消费者持有时不得提前
release；不建立第二套 workspace cleanup 或自动修复流程。

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

fast 与 planning 直接作为父图节点装配。fast 分支是 `create_agent` 编译出的唯一共享
`AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、messages channel 和官方
middleware，不维护项目自建 assistant/tool loop。

planning 分支是显式 `AssistantPlanningGraph`，其运行时角色只有：

```text
supervisor -> controls -> supervisor
supervisor -> Send(worker) * N -> join -> supervisor
supervisor -> END
```

Supervisor 是直接绑定 `load_skill`、`load_skill_reference`、`write_todos` 和 `task` 的普通 LLM node，
不是 `create_agent`。一次 `AIMessage` 只能包含一个 control call、一个或多个纯 `task(todo_id)` call，或
不含 ToolCall 的最终回答；混合、未知、重复、指向不存在或 completed Todo 的调用均 fail closed。三个 control
Tool 进入标准 `ToolNode`；`task` 只是模型可见的路由 schema，不进入 ToolNode。Supervisor 固定关闭
Provider-native search。
Supervisor 每次模型调用由独立构造函数生成专用 `SystemMessage`，其正文直接来自锁定的
`langchain==1.3.15` 模块级常量 `langchain.agents.middleware.todo.WRITE_TODOS_SYSTEM_PROMPT`，生产代码直接导入
该常量而不在仓库复制 prompt 正文；上游原文要求模型自行完成 Todo，与只有 join
可写 completed 的 A-lite 契约存在用户明确接受的已知语义冲突，运行时确定性校验仍以后者为准。Supervisor 只投影经过
官方 token-aware trimming 的父级自然对话，不回灌 planning control/task transcript；随后在最新真实用户请求前依次
临时插入 planning working-memory、MemoryContext 与 TrustedRuntimeFacts 三条独立 `HumanMessage`。working-memory
只包含 Todo、Worker result、可发现 L0 Skill catalog 的 `skill_id/description`、从受信 catalog 机械重读的 active
Skill 与成功授权的 reference 正文，并携带固定 `name`/`additional_kwargs` 来源标识；三条临时消息不写入
父 messages 或 checkpoint，模型请求最后一条始终是本轮真实用户 `HumanMessage`。

Todo 只有 `todo_id/content/status=pending|completed`，是 Supervisor working memory，不是依赖 DAG。
一次多个 task call 由 conditional edge 转换为同一 super-step 的多个 `Send("worker", ...)`。Worker 是共享
`AssistantFastAgent` 的 scoped invocation：`agent_phase="worker"` 时隐藏 Skill control Tool，保留静态业务
Tool、渐进 Skill exposure、read retry、per-Tool limit、summarization、官方 model/tool call limit 与 planning
非 read HITL，并要求严格 `WorkerResult(todo_id,status=succeeded|blocked,summary)`。每个 Worker 只获得当前 Todo、
本轮已加载的必要 Skill/reference、冻结 Memory/TrustedRuntimeFacts 和私有 messages；父 conversation 与 Worker
业务 Tool transcript 不写回父 messages。

join 不做调度决策，只在同一 wave 完整成功后合并 `WorkerResult`：succeeded 将 Todo 标为 completed，blocked
保持 pending；每个结果以原 task `tool_call_id` 生成父级标准 `ToolMessage`，随后返回 Supervisor。Supervisor
自行决定 retry、改写未来 Todo 或 finish。business blocked 是正常数据；provider timeout、连接错误、control-flow
和其他 operational exception 原样传播，LangGraph checkpoint/pending writes 在 resume 时只重跑失败分支，join
不会基于半提交 wave 执行。planning 不维护 Planner、Scheduler、Finalizer、admission、authorization envelope、
generation/attempt、budget reservation 或 recovery ledger，也不创建第二套 Runtime。


## State 与恢复

生产 state channel、checkpoint 和 reducer 调度全部使用 LangGraph 原生能力。父图继续以标准
`AgentState.messages` / `add_messages` 为事实源，并只增加 `execution_mode`、冻结的
`memory_context/memory_status` 与 `trusted_runtime_facts`。fast agent 子图使用成功 `load_skill` 产生的
`active_skill_ids` 和窄 `skill_reference_grants`。

planning 子图只保存 `todos`、按 todo ID 合并的 `worker_results`、一次 wave 的 `worker_writes` 以及上述
Skill/reference state。`write_todos` 只能创建或改写 pending Todo；completed 只能由 join 根据 succeeded result
写入，之后不得被 `write_todos` 删除、改写或降级。pending 内容变化会清除同 ID 的旧 blocked result，避免目标与
结果错配；succeeded result 相同 replay 幂等、不同覆盖冲突拒绝，blocked 可由后续 retry 的最新正常结果替换。
join 使用 `Overwrite([])` 清空
`worker_writes`。当前 Supervisor `AIMessage.tool_calls` 就是 wave 请求事实，不另存 pending task、ready set、
execution ID、attempt、generation、reservation 或 recovery state。operational exception 不写业务失败结果，
由 Agent Server/LangGraph 原生 checkpoint 与 pending writes 保存已成功分支。

coding 子图内部的 analysis channel 是 opaque `CodingAnalysisSnapshot`、三个固定 task，以及通过 state schema
显式声明的确定性 task-ID replacement reducer 所维护的有界 `CodingAnalysisResult`；该 reducer 在 checkpoint replay
时以同 task 新结果替换旧结果，不使用并行 append reducer。其余 channel 包括 `pending|completed|partial|unavailable` 状态、
`active|released|cleanup_pending` snapshot release 状态和 `analysis_context_consumed`。pending checkpoint 恢复时只
派发尚未完成的 task；已 join、repair active 或任一 approval resume 都从既有 checkpoint 继续，不创建新 snapshot
或重跑已完成分析。

coding final review 另保存 expected snapshot schema version、opaque final snapshot、固定 reviewer task、signed result、
canonical report/status、validation digest、decision context 与 audit decision。pending checkpoint 依赖 active v2
snapshot；completed checkpoint 从上述有界 contract 恢复 decision，不重放已完成 reviewer，也不把已清理的 snapshot
物理目录当作永久 mutation gate。新 coding cycle 原子清除这些 channel；terminal 清除 snapshot、input、task、result
和 decision context，只保留 canonical report、status、generation、validation/version binding 与 decision audit。

父图不投影或改写生成图片。`image_generation` 直接使用标准 `ToolMessage(content, artifact)`：模型下一次调用
只读取窄文本 `content`，程序消费者从 `artifact.images[]` 读取受管图片引用。最终 `AIMessage` 保持模型原始
回答，因此 Studio 当前只显示生成成功文本，不承诺图片预览；媒体 WebSocket 在入口适配层完成自己的 wire 投影。

实时摄像头 chat 只通过最新标准 `HumanMessage` 的 `source=live_camera` video block 进入父图；其中可以携带
视觉模块生成的可信目标边界，但 JPEG、Provider client、task 和 lease 不进入 state。父图只通过标准 ToolNode
消费视觉结果，逐帧并发、等待和晚到结果语义见视觉 authority。

A-lite planning checkpoint schema 与旧 v1/v2 planning topology 不兼容，因此生产 Graph 使用版本化
`assistant-native-v3`。Agent Server auth 按 graph-aware create 把 chat thread 的 metadata `assistant_graph_id`
规范为 v3，同时保留独立 Memory graph identity；chat run-create 与显式 graph metadata update 以 owner + v3 identity
过滤，旧 identity 不能通过更新伪装升级。旧 run 的 interrupt/rollback 只按 owner 授权，以便部署时 drain/cancel。
SDK adapter 还会在 create/stream 边界复核相同 identity。v1/v2
或缺失 identity 的 unknown thread 及其 checkpoint 只读，不能进入 v3 run/resume/replay。部署前必须 drain 或
cancel v2 pending/interrupt run；completed 历史可 inspection，但不做 planning state migration。固定 planning
assistant UUID 保留并改绑 v3，Studio 需要在该 assistant 下创建新 thread。校验函数仍接收调用方期望的 graph ID，
不阻止 Memory 等独立 Graph 使用自己的 thread 与版本身份。


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
A-lite 不定义 planning 专用 recovery custom event。Supervisor、controls、worker、join 的 state update 与嵌套
Worker model/tools 事件直接使用原生 updates/messages/subgraph stream；Graph Studio 可以看到这些父节点与 Worker
`create_agent` 子图。媒体 custom route 不订阅或重解释 planning 内部事件，也不建立 shadow event bus。


`HumanInTheLoopMiddleware` 使用 state-aware `when` predicate：fast 模式自动放行；planning Worker 对非 read
业务 Tool 在执行前触发原生 interrupt，Supervisor 的 control Tool 均为 read。恢复使用 Agent Server/LangGraph
`Command(resume=...)`，已完成 Worker 分支由 pending writes 保留，不重放。共享 fast/Worker agent 使用官方
`ModelCallLimitMiddleware`、`ToolCallLimitMiddleware`、只读 Tool retry、summarization 与 metadata-driven
per-Tool limiter；planning 父图不维护全局 budget ledger。
coding patch、final review decision 和 merge approval 各自使用独立原生 interrupt；review decision 不属于 Tool
middleware HITL，不能授权 patch apply 或 merge apply，也不能被 `unavailable` report、integration-disabled 配置或
snapshot cleanup 自动跳过。

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
  tests/tdd/supervisor-todo-planning-alite-production
```

### Stage 5C final review 运行时契约（2026-08-24）

- LOOP-001 的生产拓扑是 `apply_patch -> run_validation -> prepare_review_snapshot -> run_code_review -> coding_review_decision -> create_commit`；review 关闭时才允许 `run_validation -> create_commit`，两条路径都必须携带 validation snapshot binding，mutation lane 始终唯一且顺序执行。
- 新运行的固定 review task 仅为 `correctness_regression`、`security_governance`、`tests_validation`。worker 结构化状态仅为 `completed` / `unavailable`；finding 严格包含 `title`、`explanation`、`remediation`，severity 仅为 `critical` / `high` / `medium` / `low`，evidence digest 绑定受信 read observation 的 `content_digest`。
- 真实 reviewer 最多 8 次只读 Tool call，并允许第 9 次 model call / ToolStrategy ToolCall 产出最终结构化结果；未知 Tool 同样消耗总 ToolCall 预算，不能绕过上限。result JSON 上限 16,000 字符，canonical report 上限 48,000 字符，均按最终 signed serialization 检查。
- `coding_review_decision` interrupt 除 canonical binding context 外携带最多 12 条有界 findings summary（finding id、severity、category、title、首个 path/line）；summary 只用于展示，不是 resume binding 字段。
- `unavailable` 仍需独立 HITL 决策且不会自动 repair；current-v2 Tool observation 的 snapshot/tree/content/path binding、安全与 snapshot contract 错误在到达该状态前 fail closed。
- terminal summarize 幂等释放 validation/review snapshot，controlled commit 在 comparison success/failure 都释放 expected/current snapshot；review approve 且 integration 开启时 lease 保持 active 到 commit checkpoint，不能在 decision 后提前释放。
