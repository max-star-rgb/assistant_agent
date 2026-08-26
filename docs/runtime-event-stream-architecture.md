# LangGraph-native Assistant 运行与流式架构

最后更新：2026-08-26

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
  -> memory_recall
  -> execution_router
       fast     -> AssistantFastAgent --------+
       planning -> AssistantPlanningAgent ----+
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

Studio 可在同一 graph 上创建 owner-scoped Assistant，并通过 context `system_prompt` 定义身份、人格和任务偏好。
fast 与 planning coordinator 共用分层 Prompt Builder：稳定核心规则不可覆盖，Assistant 指令随后加入，用户
北京时间/真实地区和本轮媒体事实最后追加；入口 profile、实时媒体模式与视觉 capability 不属于公开 Assistant schema。

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

primary `inspect_and_draft` 的单 epoch 继续使用官方 `ModelCallLimitMiddleware` 与
`ToolCallLimitMiddleware` 数值预算，但预算终止采用官方 graceful counter：model limit 结束当前 Agent，tool limit
把超额调用变成标准 error `ToolMessage`。Runtime 不匹配英文错误文案，只从 middleware counter、标准
AI/Tool call ID 和受信 read Tool 清单提取不含源码的 canonical progress。首次 inspect 加最多两个 recovery epoch，
经 `evaluate_inspect_progress -> consume_inspect_recovery_context -> inspect_and_draft` checkpoint 回边复用同一
thread、workspace、base commit 与顺序 mutation lane；临时 context 只列 canonical Tool 名和仓库相对路径，不写入父
`messages`。重复 digest、读取集合/路径无新增以 `coding_inspect_no_progress` 终止，第三个 epoch 仍无 proposal 以
`coding_inspect_recovery_exhausted` 终止；base/diff/history/status/context 任一漂移以
`coding_inspect_recovery_binding_mismatch` fail closed。Provider、permission、identity、cancel、sandbox、workspace
和未知异常不进入 recovery。该恢复与 apply 后 validation repair、final review repair 正交，恢复节点不能直达任何
mutation、validation、review 或 integration 节点。

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

`memory_recall` 只召回长期记忆，并在同一次节点更新中写入 `memory_context` 与 `memory_status`；recall 最终失败时，
原生 error handler 将 Memory 标记为 `degraded`。日期与地区不属于 Memory，也不进入 Graph state/checkpoint：fast 与
planning 在每次 model call 使用原生 `dynamic_prompt`，把北京时间自然日和真实用户地区配置追加到 system prompt 末尾。
从 recall 后的 interrupt 恢复会沿用冻结 Memory，但 system prompt 会按恢复时的北京时间自然日重新生成；不提供时分秒。

fast 与 planning 直接作为父图节点装配。fast 分支是 `create_agent` 编译出的唯一共享
`AssistantFastAgent`，使用标准 `BaseChatModel`、`BaseTool`、`ToolRuntime`、messages channel 和官方
middleware，不维护项目自建 assistant/tool loop。

planning 分支是官方 `create_agent` 编译出的 `AssistantPlanningAgent`：

```text
model -> tools -> model
          task -> AssistantFastAgent
model -> END
```

Supervisor 通过官方 `TodoListMiddleware` 获得可执行 `write_todos` Tool，通过 Deep Agents
`SubAgentMiddleware` 获得可执行 `task(description, subagent_type)` Tool，并通过只绑定 Skill 虚拟根的上游
`read_file` 在拆解前读取专项知识；它不持有 `activate_tool_profile` 或业务 Tool。Todo 的
`content/status=pending|in_progress|completed` schema、更新语义和
执行逻辑均由锁定的 `langchain==1.3.15` middleware 提供；项目只通过其官方扩展参数提供中文 system prompt 与
Tool description，不再维护 Todo reducer、completed gate 或 Worker result ledger。Supervisor 固定关闭
Provider-native search。

`task` 是 Deep Agents 0.7.8 提供的真实 `StructuredTool`，不是路由占位 schema。唯一注册的
`general-purpose` 类型直接引用已经编译的共享 `AssistantFastAgent`。task 用 description 创建子 Agent 的唯一
`HumanMessage`，同时传递父 planning state 中冻结的 Memory 与 execution mode。Planner 是否读取
Skill 仍由 LLM 自主决定，并把 task 所需规则写入 description；Skill metadata、读取 transcript 和加载状态不进入子
Agent。父 conversation、Todo、Tool Profile、调用计数和 structured response 也不进入子 Agent。子 Agent 可按自身原生
循环读取 Skill、激活 Tool Profile、调用业务 Tool、
summarize 或触发 planning 模式非 read HITL。完成后 Deep Agents 只把 structured response 或最后一条非空
`AIMessage` 文本写成原 task call 对应的父级 `ToolMessage`；项目结果投影不回灌 worker Skill/Profile state 或内部
AI/Tool transcript。

同一 `AIMessage` 中的多个 task call 由 `create_agent` 内置 `ToolNode` 并行执行；fan-out/fan-in、Tool 错误、
`Command` state update 与 checkpoint 都使用上游实现。项目只为 task 并发回写的冻结字段声明“结果必须一致”的
LangGraph reducer；Planner 与 worker 的 Skills middleware state 和文件读取 transcript 保持角色局部，不合并为
Tool Profile、权限或能力授予。
主链也不再维护 controls、`Send(worker)`、join、wave、attempt、reservation 或 recovery ledger。


## State 与恢复

生产 state channel、checkpoint 和 reducer 调度全部使用 LangGraph 原生能力。父图继续以标准
`AgentState.messages` / `add_messages` 为事实源，并只增加 `execution_mode` 与冻结的
`memory_context/memory_status`。fast agent 子图只额外保存显式
`activate_tool_profile` 产生的 `active_tool_profile_ids`；上游 `skills_metadata` 是 middleware 私有 state，Skill 正文只
存在于当前角色的标准 `read_file` transcript。

planning agent 只在官方 state 中保存标准 `messages`、`todos`、冻结的 Memory、execution mode 和
上游 middleware 私有的 `skills_metadata`；它不保存或激活 Tool Profile，也没有项目 Skill/grant channel。
Todo 不含项目 `todo_id`，也没有 `worker_results` 或 `worker_writes`
channel。task 调用、结果与 Todo 更新都作为标准 AI/Tool transcript 进入 checkpoint；子 Agent 私有 transcript
不进入父 state。恢复、并行 Tool pending writes 与错误语义均由 `create_agent`/`ToolNode`/Agent Server 所有。

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

当前原生 planning state 与已删除的 A-lite planning checkpoint 不兼容；已存在的 A-lite v3 planning thread
不做 state migration，Studio 与客户端必须新建 thread。生产 Graph 继续使用版本化
`assistant-native-v3`。Agent Server auth 按 graph-aware create 把 chat thread 的 metadata `assistant_graph_id`
规范为 v3，同时保留独立 Memory graph identity；chat run-create 与显式 graph metadata update 以 owner + v3 identity
过滤，旧 identity 不能通过更新伪装升级。旧 run 的 interrupt/rollback 只按 owner 授权，以便部署时 drain/cancel。
SDK adapter 还会在 create/stream 边界复核相同 identity。v1/v2
或缺失 identity 的 unknown thread 及其 checkpoint 只读，不能进入 v3 run/resume/replay。部署前必须 drain 或
cancel v2 pending/interrupt run；completed 历史可 inspection。固定 planning
assistant UUID 保留并改绑 v3，Studio 需要在该 assistant 下创建新 thread。校验函数仍接收调用方期望的 graph ID，
不阻止 Memory 等独立 Graph 使用自己的 thread 与版本身份。


完整 Tool inventory 仍静态注册给 fast `create_agent` 的 `ToolNode`；通用 `ToolProfileMiddleware` 自带
`activate_tool_profile`，每次 model call 的可见子集由受信静态 profile catalog 与当前 invocation 的激活状态派生。
Skill 只提供渐进知识，不参与 Tool 授权；未归属 profile 的 Tool 保持独立可见。该过滤不创建第二套 Tool runtime，
也不改变 ToolNode 对已注册 Tool 的标准执行路径。

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
planning 不定义专用 recovery custom event。Graph Studio 的固定父级路线显示
`planning_agent -> model/tools`；每次具体执行路线由 `task` Tool 的嵌套 subagent run、messages/updates 和 LangSmith
trace 展开，不再显示固定 controls/worker/join 节点。媒体 custom route 不订阅或重解释 planning 内部事件，也不
建立 shadow event bus。


`HumanInTheLoopMiddleware` 使用 state-aware `when` predicate：fast 模式自动放行；planning task 内的 fast 子 Agent
对非 read 业务 Tool 在执行前触发原生 interrupt。恢复使用 Agent Server/LangGraph `Command(resume=...)`。
共享 fast 子 Agent 使用官方
`ModelCallLimitMiddleware`、只读 Tool retry、summarization 与参数级 per-Tool limiter；每个 invocation 最多 12 次
model call，不设全 Tool 总调用上限，每个 Tool 同一规范化参数最多执行一次、不同参数最多执行 12 次，metadata 可声明
更低上限。planning coordinator 使用相同边界，不维护全局 budget ledger。
coding patch、final review decision 和 merge approval 各自使用独立原生 interrupt；review decision 不属于 Tool
middleware HITL，不能授权 patch apply 或 merge apply，也不能被 `unavailable` report、integration-disabled 配置或
snapshot cleanup 自动跳过。

## 已退役兼容边界

旧 assistant loop、Graph app、通用 Runtime facade、Workflow host 与旧 checkpoint/Memory node bundle 已删除。
`src/assistant_agent/runtime/` 只保留仍被 Tool、Provider、媒体、Context 或 durable task 使用的中立 DTO；
Registry/Executor、产品事件投影与零消费者 Runtime DTO 已删除，它不拥有 Graph 生命周期。主动投递的中立
DTO/Store 位于 `assistant_agent.proactive_delivery`。

评测侧保留直接调用本生产父图的 `NativeGraphEvaluationTarget` 基元，并由 Stage 5E
`ai_coding_behavior` runner 通过生产 Agent Server 的标准 thread/run/checkpoint/interrupt/resume 生命周期建立
CodingGraph 行为基线。evaluation contract、fixture、deterministic grader、artifact 与 operator attestation 门禁
不拥有产品状态机，不得改变节点路由、审批语义或把 Provider-native code execution 当作 repository validation。
旧 Runtime/Workflow/Release Review runner 仍保持删除，不通过兼容 facade 恢复。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/tdd/native-deepagents-planning
```

### Stage 5C final review 运行时契约（2026-08-24）

- LOOP-001 的生产拓扑是 `apply_patch -> run_validation -> prepare_review_snapshot -> run_code_review -> coding_review_decision -> create_commit`；review 关闭时才允许 `run_validation -> create_commit`，两条路径都必须携带 validation snapshot binding，mutation lane 始终唯一且顺序执行。
- 新运行的固定 review task 仅为 `correctness_regression`、`security_governance`、`tests_validation`。worker 结构化状态仅为 `completed` / `unavailable`；finding 严格包含 `title`、`explanation`、`remediation`，severity 仅为 `critical` / `high` / `medium` / `low`，evidence digest 绑定受信 read observation 的 `content_digest`。
- 真实 reviewer 最多 8 次只读 Tool call，并允许第 9 次 model call / ToolStrategy ToolCall 产出最终结构化结果；未知 Tool 同样消耗总 ToolCall 预算，不能绕过上限。result JSON 上限 16,000 字符，canonical report 上限 48,000 字符，均按最终 signed serialization 检查。
- `coding_review_decision` interrupt 除 canonical binding context 外携带最多 12 条有界 findings summary（finding id、severity、category、title、首个 path/line）；summary 只用于展示，不是 resume binding 字段。
- `unavailable` 仍需独立 HITL 决策且不会自动 repair；current-v2 Tool observation 的 snapshot/tree/content/path binding、安全与 snapshot contract 错误在到达该状态前 fail closed。
- terminal summarize 幂等释放 validation/review snapshot，controlled commit 在 comparison success/failure 都释放 expected/current snapshot；review approve 且 integration 开启时 lease 保持 active 到 commit checkpoint，不能在 decision 后提前释放。

### Stage 5D review repair 运行时契约（2026-08-24）

- `coding_review_decision` 的 `respond` 只接受当前完整 `findings` report 与非空、有界文本；`clean` / `unavailable` 仍只能 approve/reject。decision 继续绑定 workspace/generation/base commit、validation evidence、diff/tree、review report 与 snapshot schema，旧 interrupt、重复消费或任一 binding 漂移都 fail closed。
- 合法 respond 先原子冻结最多 12 条 findings 的有界 repair context，并使旧 patch、validation、review 与 integration authorization 失效；随后必须依次经过独立 checkpoint 节点 `consume_review_repair_budget`、`consume_review_repair_context`，才可把该 context 一次性投影给既有 `inspect_and_draft`。review repair 固定最多两轮且与 validation repair budget 独立；第三次 respond 不调用模型、不产生 patch，进入 exhausted terminal。
- 新 proposal 不能复用旧授权，必须重新执行 proposal validation、patch approval、apply、deterministic validation、final snapshot、只读 review 与 review decision。累计 approved path inventory 在 mutation 前与受信 live workspace 精确复核，mutation 后只允许在旧 inventory 与当前批准 proposal paths 的并集中安全收敛；reviewer 仍只持有 snapshot-bound read Tools，不获得 patch、validation、commit、merge 或 approval authority。
- respond 后不再需要的 validation/review snapshot lease 必须确定性、幂等释放；释放失败只记录 `cleanup_pending` 供既有 owner reaper 收敛，不掩盖原 decision/resume 错误。START、resume 和 non-terminal checkpoint 在任何 workspace/apply side effect 前校验 repair count/history lineage、一次性 context、互斥 authorization channel、path inventory 与 workspace/digest/schema/identity/permission binding；stale、orphaned 或恶意组合均 fail closed。

### Stage 5D final review repair invariants

Stage 5D 的 review repair checkpoint 必须绑定完整、可重建且不可变的决策来源，而不是只绑定 finding ID。`CodingReviewRepairContext` 同时冻结 canonical findings projection digest、context digest，以及 `workspace_ref`、`base_commit`、generation、snapshot ref/schema/timestamps、tree digest、workspace diff digest、patch digest、validation evidence digest、report digest 和 response digest；history attempt 必须绑定该 context digest。任一 report、projection、history 或 workspace lineage 漂移均 fail closed。

在消费 repair budget、消费 repair context 和向 inspect model 投影上下文前，Runtime 必须从 live workspace 重新 materialize immutable snapshot，并与冻结的 tree/workspace-diff/content identity 对照；仅检查 checkpoint state 或路径清单不足以授权 model call 或 budget side effect。临时复核 lease 在比较后释放，cleanup 失败只记录 `cleanup_pending`，不得替换原业务结论。`legacy_v1` 兼容只允许既有的 schema materialization 差异，不放宽 content identity。

review `respond` 接受时必须在 fresh snapshot 内容身份复核成功后，从同一 live workspace 取得 canonical source dirty-path inventory；该 inventory 写入 repair context、attempt、context/history digest 与 decision audit，并由 source validator 复核，不能从可累积、可伪造的 `approved_changed_paths` reducer state 取得。repair patch apply 前必须重新取得 live dirty paths，且只允许其属于“canonical source inventory 与当前 approved proposal paths”的并集；因此真实 source snapshot 已经消失的 ghost path 不会阻塞修复，而 checkpoint 同时伪造 path inventory 和 live 新路径仍在 apply side effect 前 fail closed。apply 后 actual dirty paths 继续以同一安全并集为上界收敛，并覆盖 checkpoint inventory。

repair validator 按阶段校验 authorization channel：pending 和 projection checkpoint 不允许残留 current review、integration 或 patch approval channel；未 apply 的 active proposal 只允许与 latest `proposed` history 一致的唯一 patch authorization；已 apply 后才允许完整、由既有 review/integration validator 继续认证的 current review 或 integration channel。单个孤立字段、伪造 approval 或跨阶段混合状态一律 fail closed；明确合法的 consumed canonical projection checkpoint 必须可重放。

Stage 5D repair 中既有 patch-approval HITL 的 `respond` 必须原子进入显式 `redraft` phase：清除 draft/proposal/validation、approval status/context/digest/origin 及全部下游 patch authorization marker，只保留规范化、有界且一次性投影的 user feedback 与 active canonical repair lineage。下一次 inspect 在投影 feedback 或调用 model 前，必须重新 materialize live immutable snapshot，并与该 lineage 冻结的 tree/workspace-diff/content identity 对照；同一已授权路径发生外部字节漂移也必须 fail closed，临时 lease 的 release/`cleanup_pending` 语义与首次 repair projection 相同。即使 live binding 与 model 业务结果成功，release 产生的 cleanup debt 也必须合并进 checkpoint owner 状态，失败或重放不得丢失清理责任。live binding 通过后才可合法消费 feedback，产生的新 proposal仍必须重新进入完整 patch approval；`redraft` phase 中任何孤立 approval marker 继续 fail closed。

redraft 的 live identity check/release 与 inspect model 必须由两个独立、顺序 checkpoint 隔开：前一 checkpoint 在任何 model side effect 前提交 canonical context/feedback-bound ready digest 和合并后的 lease cleanup status；后一 checkpoint 只消费该 ready digest，不重新 materialize snapshot。model 异常或 model node update 未提交时，重放必须继续持有同一 cleanup debt，且不得重复创建临时 lease；model update 成功提交后才清除 ready digest。

所有含 review repair history 的 terminal 都必须把 latest attempt 终结为 `terminal` 或 `exhausted`，并原子清除 active status、context 和 projection。`exhausted` 还必须保留最终 canonical review report、decision summary 与 validation evidence 供审计，并在 terminal cleanup 中释放不再需要的 snapshot lease；非 exhausted 终态不得把旧 source report 重新投影成当前 public review report。
