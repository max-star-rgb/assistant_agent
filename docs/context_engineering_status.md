# 上下文工程架构

Last updated: 2026-08-08

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Assistant context 构建、编译、预算与压缩的当前权威 |
| Owns | `AssistantContextPack`、conversation、memory/tool 投影、prompt 编译、budget、compaction、report |
| Does not own | Tool 选择与授权、Memory 提取持久化、Gateway 生命周期、Provider 执行与 trace schema |
| 源码与 schema 入口 | `src/assistant_agent/context/`、`src/assistant_agent/runtime/system_prompt_policy.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

本文是 assistant context 的当前权威入口，描述稳定职责、生命周期、跨模块契约和失败语义。
它不记录阶段进展、历史变更或下一步事项。具体默认值、内部类型和实现细节以源码、配置和测试为准。

涉及 prompt/context rendering、conversation history、memory context、tool observation、
context budget 或 compaction 的任务先读本文；若文档与源码或测试不一致，以源码和测试为准并回补本文。

## 1. 职责与边界

Context engineering 负责：

- 将当前请求、短期对话、长期记忆快照、受信运行时状态、工具结果和候选工具 schema
  组装成 `AssistantContextPack`。
- 将 context pack 编译为 Provider-native `ChatRequest`，并维护消息角色、来源、优先级和数据边界。
- 按完整 Provider 请求计算输入预算，治理 conversation compaction 和 Provider overflow。
- 只把 prompt-safe 的工具结果及外部上下文投影给模型。
- 生成脱敏、可审计但不可用于 prompt replay 的 context report。

Context engineering 不负责：

- 推断用户工具意图、授予工具权限或绕过
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- 实现长期记忆的提取、合并、排序、向量化或持久化。
- 承担 Gateway 的 session/run/cancel/interrupt/reconnect 生命周期。
- 把 durable task、realtime task state 或父 Agent 历史隐式转换为模型权限。
- 保存或暴露 raw Provider payload、secret、原始媒体、完整 prompt 或 hidden reasoning。

## 2. 编译生命周期

每次 assistant model call 遵循同一条主路径：

1. 入口归一化 `UserRequest`，运行时加载可信的 session conversation、memory snapshot
   和显式启用的 runtime context。
2. `ContextBuilder` 生成 Provider-neutral 的 `AssistantContextPack`。
3. `PromptCompiler` 结合当前运行阶段和已治理的 `ToolSpec`，生成完整 `ChatRequest`。
4. `ContextTokenCounter` 对实际请求的稳定 payload projection 做 tokenizer preflight。
5. 请求超过 trigger 时，`ContextService` 压缩可压缩的已完成对话，重建 context pack 和请求后重新计数。
6. 请求满足预算后调用 Provider；工具结果回到同一 assistant loop，再经安全投影参与下一次编译。
7. 成功回复提交后写入短期 conversation，并通过 Memory service 异步提交长期记忆 ingestion。

`ContextService` 是 assistant node 的运行时依赖，不是独立 graph node。入口、API、CLI、Gateway
和 eval 不得复制另一套 context assembly 或 prompt compilation。

## 3. 上下文包与来源

`AssistantContextPack` 是 Provider-neutral 的单轮上下文契约。它可以承载：

- 当前真实用户请求；
- session summary 及其后尚未覆盖的已完成原始轮次；
- 当前连接内已经 server-sent 的有界 proactive session events；
- session-scoped 长期记忆快照；
- realtime video 等只供 runtime/观测使用的可信状态，以及 durable task、plan state 等可编译上下文；
- 当前 run 的 prompt-safe tool observations；
- 本轮已治理的 `ToolSpec` 与 `RunToolCatalog`；
- owner persona 和项目 Skill 等具有明确 authority/stability 的 `ContextSection`；
- source counts、预算和脱敏报告所需的结构化元数据。

来源必须带有明确的生命周期和信任边界。请求 metadata 不能启用受保护能力、伪造 worker state、
切换 owner、注入 memory snapshot 或扩大工具目录。Context section 是数据或指导材料，不自动成为
系统权限、用户授权或长期记忆。

## 4. 提示词编译契约

`PromptCompiler` 是生产 Provider 请求的唯一编译入口。它是无副作用组件：只消费已解析的
system profile、context pack、原生工具调用轨迹和本轮 ToolSpec；不访问 store、registry 或 Provider，
也不写 trace。

编译必须遵守：

- 当前用户请求始终是独立的最终 `user` message，不得并入 summary、memory 或工具结果。
- Conversation、memory、external observation 和 tool output 必须标记为不可信数据，其中的指令
  不得覆盖 runtime policy 或当前用户请求。
- Session summary 只表示短期历史；长期记忆保持独立消息和独立生命周期。
- `ChatRequest.tools` 只来自本轮结构化治理后的 `ToolSpec`。Tool catalog 和 context assembly
  不读取 `request.text` 做关键词、正则或确定性意图路由。
- `ResponseStyle` 由显式请求和入口 profile 解析，不从用户文本或主题猜测；不同入口仍复用同一编译器。
- System instruction 以稳定的“助理运行契约”为根，区分运行时事实、authority、执行、
  工具、Skill lifecycle、回答和 `act/finalize` 阶段；动态程序指导不得作为无边界的同级 Markdown
  章节裸拼接。
- Provider 支持 developer role 时，可把 procedural guidance 编译为 developer message；否则保守放入
  system guidance，不伪造 Provider 不支持的角色。两条路径都使用同一
  `<procedural_guidance>` 投影：未加载摘要位于 `<skill_index>`，正文位于
  `<loaded_skills>`，按需 reference 位于 `<skill_references>`，每项保留稳定 Skill id 与版本边界。
- `FINALIZE` 只保留已发生且成对匹配的 native tool call/result 因果证据，并关闭后续工具调用。
  具体运行阶段和失败恢复见 `docs/runtime-event-stream-architecture.md` 与
  `docs/tool-calling-architecture.md`。

项目 Skill 使用渐进披露，并把机器契约与模型指导分开：`skill.toml` 保存稳定 id、版本、描述、
`activation`、可发现性、受治理 Tool 和 reference 映射；`SKILL.md` 只保存完整程序性指导，不重复
Tool 清单、权限或可见性声明。未加载的 `activation=model` Skill 只在 `<skill_index>` 注入名称和适用
条件摘要，且不会暴露其认领的业务 Tool；任务符合一个或多个 Skill 的适用条件时，模型必须先通过受
治理的 `load_skill` 加载直接相关正文。成功结果由 Runtime 转成会话级 `CapabilityGrant`，同一 run 的
下一次模型调用以及同 owner/agent/session 的后续 turn 才可看到该 Skill 正文和仍满足本轮结构化资格的 Tool。

`activation=context` Skill 不进入索引且不能由模型加载；它只在已有 entry/media/env 结构化事实满足
受治理 Tool exposure 时由 Runtime 自动激活，适用于图片、视频帧和实时画面等上下文能力。Skill grant
当前在会话内持续保留，不设 TTL 或清除；恢复时以当前 `skill.toml` 重建能力。调用方 metadata 不能
伪造或预选 Skill。reference 只能从正文加载结果实际返回的 `reference_ids` 中按需加载，整个加载过程
静默且不向用户播报。动态正文仍是 `ContextSection`；编译器渲染来源边界，但它不能绕过入口限制、
媒体要求、ToolSpec policy、用户授权或 validator 结果。

## 5. 对话与压缩

短期对话由 `ConversationStore` 按 session 管理，普通 turn 与 rolling summary 分开持久化。
Summary 只用于恢复当前 session，不写入长期 memory。

Conversation compaction 遵守以下不变量：

- 只压缩旧 summary 和已完成的原始 user/assistant turns。
- 当前请求、当前 run 未闭合的 tool call/result、system policy、memory、tool schema 和其他
  context section 不进入 conversation summary。
- 当前 run 的 native tool call/result 必须保持成对和有序，不得因压缩拆散。
- 压缩成功后，被 summary 覆盖的原始轮次才可从 conversation store 删除。
- 压缩失败不得静默删除原文或使用确定性摘要冒充成功结果。

默认预算窗口为 target 40%、trigger 70%、hard 85%，并保留 safety margin；运行配置可以覆盖具体值。
Trigger 到 hard 之间压缩失败时保留原文继续；hard 区间重试后仍无法收敛时，必须在主模型调用前
返回稳定、可解释的错误。Provider context overflow 统一进入同一 hard compaction 流程，不建立
旁路重试策略。

Mock/offline 默认不调用压缩模型。Real 模式使用 LLM compactor 时必须显式配置 compactor、
匹配目标 endpoint 的输入上限和本地 tokenizer 资产；tokenizer loader 不得联网下载。
Tokenizer accounting 与 compactor 生命周期相互独立：Real 模式只要配置本地 tokenizer 资产，
即使 compactor 关闭也必须执行完整 compiled request preflight。进入 soft trigger 但没有 compactor 时
保留原文继续；进入 hard 区时必须在 Provider 调用前稳定阻断。只有启用 LLM compactor 时 tokenizer
才是启动必需配置；其他模式缺失 tokenizer 时 token accounting 明确标记为 unavailable。

Conversation recent window 优先复用同一个目标模型 tokenizer。仅在 tokenizer 不可用的 mock/offline
路径使用确定性的字符启发式 estimate，并将 `conversation_context_token_aware` 标为 false；estimate
只用于报告或离线选择，不能作为完整 Provider request 的 hard-limit 依据。

## 6. 记忆上下文

Context engineering 只消费 `MemoryPluginHost` 提供的结构化、按 run 冻结的 snapshot：

- Session 创建时，Host 以可信身份打开唯一 active Memory Plugin，并冻结 Plugin 返回的 session
  baseline；Mem0 只是默认内置 Plugin 的私有 adapter。
- 每个 user turn 最多调用一次 `prepare_context()`；Host 将本轮完整 contribution 与 baseline 按
  `memory_id` 合并，并为当前 run 冻结结果。同一 ReAct run 的后续模型与工具迭代只复用该副本。
- Plugin 不支持 context refresh、session 尚未打开或召回失败时，Host 分别使用 baseline 或可解释的
  空/降级 snapshot，不让 Memory 故障阻断当前回答。
- Snapshot 作为独立的合成 `user` 数据消息进入 prompt；当前真实用户请求仍是后一条独立消息。
- 合成 memory 消息不写入 `ConversationStore`，也不作为原始 user message 再次提交给 Memory Plugin。
- 成功回复交付后，原始 user/assistant messages 由 Host 通过有界后台队列交给 active Plugin 的通用
  `ingest_turn()` 生命周期。

Active Memory Plugin 拥有提取、合并、排序、向量化和持久化算法。Context/runtime 不实现第二套
ranking、promotion、profile、冲突处理或 memory tool。完整 Memory 契约见
`docs/memory-service-architecture.md`。

## 7. 工具观察结果

Runtime 保留完整内部 `ToolResult` 供执行、trace 和交付层使用；进入模型的副本必须先投影和清洗。

- 工具通过 `ToolResult.model_observation` 定义业务相关的模型投影。
- Context 层只负责公共协议、安全清洗和容量限制，不解释工具私有业务字段。
- Prompt-safe observation 保留模型继续推理所需的 status、summary、结构化 data、completion/error
  事实及安全的 output reference。
- Secret、raw Provider/file/media payload、base64/data URI、HTML/raw body 和内部命令输出必须移除
  或有界压缩。
- 投影不得修改 graph state 中的原始 observation，也不得把交付协议或展示模板重新塞回 prompt。

工具选择、恢复、幂等和副作用治理仍归 `docs/tool-calling-architecture.md`。

## 8. 专项上下文边界

### 持久化任务

只有可信 worker resume 可以注入校验后的 durable task snapshot。模型只接收当前执行所需的
objective、constraints、plan/step 状态、artifact references、等待状态和剩余预算；lease、secret、
raw Provider response、父会话历史及未登记扩展不得进入 prompt。Durable snapshot 是当前执行状态，
不是 session summary 或长期记忆。

通用 Durable Workflow 不把完整前台会话或 Workflow Store JSON 回放给模型。Worker 为每个 work item
生成 `WorkflowContextManifest`，只包含 objective、constraints、owner 校验后的 artifact ref、digest 和
有界 excerpt；lease、revision、绝对路径、Store client 和完整来源正文不进入 prompt。artifact 内容由
`LocalWorkflowArtifactStore` 独立持久化，Context Compiler 施加 per-artifact 和 total char budget。

`AgentGraphRuntime.run_work_item()` 使用 runtime-owned `_trusted_workflow_assignment` 和显式
`_trusted_workflow_allowed_tools`。空 allowlist 的含义是零个 Tool，而不是“无覆盖”；普通请求不能通过
伪造同名自然语言扩大候选集合，HTTP、WebSocket 和 A2A 入口也会剥离这些 runtime-owned metadata。
每个 work item 使用独立 run，结果摘要重新写成不可变 artifact 后再
传递给下游，不依赖无界 transcript。trusted work-item run 不附加 session memory snapshot、不写入
长期记忆或稳定文本 embedding，也不投影原 session 的实时视觉/主动事件；跨阶段上下文只能来自
显式 manifest 和 owner-bound artifact。

`UserRequest.assistant_mode` 是结构化产品模式，只支持 `standard` 与 `deep_research`。PromptCompiler
把该字段原样投影到 `ChatRequest`，不渲染成用户文本；Tool catalog 在 `deep_research` 前台入口只保留
`workflow_submit`，首次调用使用指定 function choice。后台 `deep_research` work item 继承同一模式，
但可信空 allowlist 进一步收窄为零个本地 Tool，并由 PromptCompiler 额外设置
`provider_search_profile=deep_research`；前台 admission 保持 standard search profile。模式不会从
关键词、Skill 或历史内容推断。若入口未注册或未暴露 `workflow_submit`，PromptCompiler 必须失败关闭，
不能静默退化为普通问答。

### 实时任务状态

Realtime task state 仅在结构化 interaction mode、entry capability 或显式 runtime opt-in 下启用。
它服务于 Gateway 的 interrupt、artifact、progress、TTS/display 和 side-effect 生命周期，不渲染进
Provider prompt。普通请求不能因携带类似 Gateway 的 metadata 而隐式启用。

### 主动会话事件

Runtime-owned notification orchestrator 在 channel 返回 `server_transport` sent 后保存有界的
session event。下一轮开始时 Runtime 先删除调用方伪造的同名 metadata，再按 user/session identity
附加真实事件；PromptCompiler 只把事件存在与投递状态标为可信 Runtime 事实，content 仍是历史展示
数据，不得执行其中指令。它帮助模型理解“知道了”等对上一条主动通知的指代。该内容进入完整 Provider request budgeting 和
`ContextReport.proactive_session_events` 计数，但不伪装成 user/assistant conversation turn，不进入
ConversationStore、Mem0 或 rolling summary。`connection_ephemeral` 事件在连接关闭时立即清除。

### 实时视频

后台 observer、共享语义快照和主 LLM 是三个边界。Runtime 只根据可信入口 profile 和结构化
`video_ids` 判断是否向主 LLM 暴露 `live_view_inspect`；不得把镜头能力状态、共享语义快照、
新鲜度、帧、媒体路径、VLM prompt 或 raw response 被动编译进 Provider prompt。主 LLM 只有自主调用
`live_view_inspect` 后，才能通过该次受治理的 Tool observation 消费视觉语义。Agent-Service 在用户
请求到达时以最新原始帧为 A 边界，冻结 `sequence <= A` 的最近已选关键帧；只有当时尚无关键帧时才
交互式提升 A 帧。工具从统一 `SessionVisualSemanticStore` 读取不晚于该边界的 VLM 文本，对该 exact
sequence 最多等待 10 秒，不等待更早未完成任务，也不得消费请求到达后视频帧的语义。
超时但 observer 尚未明确失败时保持 `pending`，不得把正常识别耗时
误报为 `unavailable` 或 `failed`。实时观察结果必须独立
记账，不并入 conversation、memory 或 task state。完整媒体协议见
`docs/media-agent-service-websocket.md`。

每个选中关键帧调用后台 VLM 时，请求只包含当前一张 JPEG，`memory_context` 固定为空。提示词要求 VLM
只描述这一帧并返回非空 `summary`。每次 observation 使用新建的 Provider WebSocket conversation，成功、
失败或不完整响应后都关闭连接，因此 Provider 不会隐式携带上一张图片、上一轮文本或回复。每个已选
关键帧立即启动独立 task，并拥有独立 ToolRegistry、adapter 和 WebSocket；并行任务可乱序完成，旧
sequence 后完成时只补入时间线，不回退 latest snapshot。当前
Agent-Service realtime observer 不构造或调用 `VisualContextService`；仓库中保留的视觉压缩模块、配置和
事件仅用于独立兼容代码及专项测试。

成功的单帧文本按 `frame_sequence` 和 `captured_at_ms` 累积为 `VisualSemanticRecord`。主 LLM 自主调用
`live_view_inspect(query=...)` 后，工具以冻结的目标 sequence 为 as-of 边界，选择不晚于该边界的最新
证据帧，把主 LLM 提供的具体 `query` 和且仅和这一张 JPEG 交给前台 VLM；VLM 的 query-specific
`summary` 作为工具主要答案。工具同时返回最近 8 条按时间排序的后台
`[{timestamp_ms, text}]` 和 freshness，供主 LLM理解短时连续性。未来帧不能进入该列表或前台 VLM。

时间线不被动进入主 Agent prompt、`ConversationStore` 或 Mem0，也不作为下一次后台 VLM 输入。
`visual_memory_search` 按可信 as-of/time window 从原始 `VisualSemanticRecord` 读取最后最多 256 条
`[{timestamp_ms, text}]`，不做 embedding、相似度排序或命中判断。Tool 尾部的
`VisualTimelineContextService` 用专用 tokenizer budget 复用 `ContextWindowPolicy` 的
target/trigger/hard 控制：低于 trigger 时主 LLM 阅读完整列表；触发后读取 `timeline_summary`、coverage、
query-relevant 原始证据和最近原文。compactor 只能返回 source indexes，代码映射回精确原文；hard 区间
无法收敛时返回 `visual_memory_context_hard_limit`。

Context 的通用安全投影不再按固定元素数截断任何安全列表；Tool 自己已经选择的证据、摘要、coverage
和结构化计数必须整体进入完整 request budgeting。敏感字段、inline media、私有路径和单段超长文本
仍按安全策略处理。主 `ContextService.preflight` 随后继续计算 conversation、memory、tool schema 和
所有 observations，真正超过主模型 hard window 时仍明确阻断 Provider 调用，而不是静默删除列表尾部。

Session visual history 同样不被动进入 prompt。只有 Runtime 根据同 user/session 语义存储写入可信
`_trusted_visual_memory_available` 后，`visual_memory_search` 才进入 Tool catalog；调用方 metadata
会先被覆盖，exposure 不检查请求文本。模型只拥有 query/time window/search mode，session 与 as-of
由 Runtime/ToolContext 绑定。ASR 已在上游变为普通 final text，不存在语音 embedding prompt 通道。

### 可编辑所有者上下文

Owner context 默认关闭，只能由进程配置和可信 owner identity 启用。Loader 必须执行 root containment、
文件类型、symlink、编码、容量和敏感内容校验；非法新版本只能使用同 owner 分区的 last-known-good
或省略。Owner persona 可以影响表达，不能改变工具权限、identity、memory policy 或 provider mode。

### 跨 Agent 委派

子 Agent 只接收显式 `context_refs`、子任务预算和脱敏审计摘要。父 conversation、memory context、
raw tool results、Provider payload、secret 和任意未列入 allowlist 的 metadata 不向下传递。
Delegation context 不取代子运行自己的 `AssistantContextPack`。完整路由契约见
`docs/agent-communication-routing.md`。

## 9. 预算、失败与可观测性

Budget 必须按完整 compiled request 计算，包括 messages、tools、tool choice 和 response format；
字符估算只能用于预编译报告，不能冒充 tokenizer 计数。Provider 私有 chat template 的差异由
safety margin 和调用后的 usage 误差观测吸收。

Token 预算治理与局部资源治理使用不同单位：模型窗口准入、conversation 选择及 compaction 阈值按
token；文件读取、日志/trace、单字段安全投影和异常 payload 防护仍可按 byte、char、line 或 item
限制。局部字符限制不能替代最终 compiled request token preflight，字符观测字段也不应重命名为
token 字段。

容量治理优先保持因果完整性和证据：

- ContextBuilder 不得用全局字符上限静默裁剪 conversation、memory 或 tool observations。
- Conversation 使用 rolling compaction；tool observation 和专项 context 使用各自的安全投影与局部上限。
- 如果不可压缩的 system、memory、tool schema、durable state 或当前 run 证据仍超过 hard limit，
  Runtime 返回稳定错误，不发送已知超限的 Provider 请求。

`ContextReport` 是脱敏的调试/审计摘要，不是 prompt replay。它可以报告 section accounting、
selected tools、source counts、compaction 状态和 tokenizer preflight，但不得返回原始 prompt、
memory 文本、完整 tool observation、raw Provider payload 或 secret。Token 不可用时必须明确标记
为 unavailable，不能用零伪装。

`ContextBudgetReport.procedural_guidance_chars` 记录原始 `ContextSection.content` 的来源字符量，不包含
编译阶段增加的 `<procedural_guidance>` envelope、属性和 XML entity 开销；该字段用于来源 accounting，
不是模型窗口准入值。最终 compiled request tokenizer preflight 和 system/developer prompt report 必须包含
这些渲染开销，并继续作为 hard window 的权威口径。

Langfuse 中 `context.compile` 只把 tokenizer preflight 投影为 observation metadata，不计入 Usage
breakdown；实际 input/output/total usage 只归属随后对应的 `llm.chat` generation，防止同一 Provider
调用重复计量。

Canonical `context.build.started` / `context.build.finished` 记录 context 编译生命周期；
最终 compiled `ChatRequest` 只归属对应的 `llm.chat` generation input。查询和脱敏规则见
`docs/observability-harness.md`。

## 10. 验证与权威导航

`tests/core/integration/test_context_lifecycle.py` 保护已登记的 context core invariant，包括预算、
compaction 失败分级、compiled accounting 和 native tool call/result 配对。更具体的 feature 检查
位于 `tests/tdd/**` 或 `evals/system/incubating/**`；tokenizer 误差和摘要语义质量属于 system eval 或上线前 Release Review，
pytest 不调用真实 Provider。

相关权威：

- Runtime stream、assistant loop、`ACT` / `FINALIZE`：`docs/runtime-event-stream-architecture.md`
- Tool catalog、Skill loading、执行与副作用治理：`docs/tool-calling-architecture.md`
- Memory 生命周期与 Mem0：`docs/memory-service-architecture.md`
- Gateway 与 realtime task state：`docs/gateway-architecture.md`
- Realtime media：`docs/media-agent-service-websocket.md`
- Multi-agent delegation：`docs/agent-communication-routing.md`
- Trace 与 redaction 契约：`docs/observability-harness.md`
- 真实运行诊断：`docs/observability-diagnosis-runbook.md`
- 测试与 eval 分层：`tests/README.md`、`evals/README.md`

实现入口按职责从 `src/assistant_agent/context/`、`src/assistant_agent/runtime/`、
`src/assistant_agent/memory/`、`src/assistant_agent/media/` 和
`src/assistant_agent/multi_agent/` 定位；不要在权威文档中维护易漂移的内部文件清单。
