# Context Engineering Architecture

Last updated: 2026-08-05

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

## 3. Context Pack 与来源

`AssistantContextPack` 是 Provider-neutral 的单轮上下文契约。它可以承载：

- 当前真实用户请求；
- session summary 及其后尚未覆盖的已完成原始轮次；
- session-scoped 长期记忆快照；
- realtime video 等只供 runtime/观测使用的可信状态，以及 durable task、plan state 等可编译上下文；
- 当前 run 的 prompt-safe tool observations；
- 本轮已治理的 `ToolSpec` 与 `RunToolCatalog`；
- owner persona 和项目 Skill 等具有明确 authority/stability 的 `ContextSection`；
- source counts、预算和脱敏报告所需的结构化元数据。

来源必须带有明确的生命周期和信任边界。请求 metadata 不能启用受保护能力、伪造 worker state、
切换 owner、注入 memory snapshot 或扩大工具目录。Context section 是数据或指导材料，不自动成为
系统权限、用户授权或长期记忆。

## 4. Prompt 编译契约

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
- Provider 支持 developer role 时，可把 procedural guidance 编译为 developer message；
  否则保守放入 system guidance，不伪造 Provider 不支持的角色。
- `FINALIZE` 只保留已发生且成对匹配的 native tool call/result 因果证据，并关闭后续工具调用。
  具体运行阶段和失败恢复见 `docs/runtime-event-stream-architecture.md` 与
  `docs/tool-calling-architecture.md`。

项目 Skill 使用渐进披露：未加载时只注入名称和适用条件摘要；任务符合某个 Skill 的适用条件时，
模型必须先通过受治理的 `load_skill` 加载正文，不能因任务简单或预计只调用一个业务工具而跳过。
不相关 Skill 不加载，reference 只按需加载，加载过程不向用户播报。动态 Skill 正文仍是
`ContextSection`，不能改变 ToolSpec、工具权限或 validator 结果。

## 5. Conversation 与 Compaction

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

## 6. Memory Context

Context engineering 只消费 Memory service 提供的结构化 session snapshot：

- Session 创建时，Memory service 按可信身份从 Mem0 加载一次并冻结 snapshot。
- 同一 session 的所有 turn 复用该 snapshot；缺失或读取失败时使用空记忆，不在 turn 中隐式重试召回。
- Snapshot 作为独立的合成 `user` 数据消息进入 prompt；当前真实用户请求仍是后一条独立消息。
- 合成 memory 消息不写入 `ConversationStore`，也不作为原始 user message 提交给 Mem0。
- 成功回复提交后，user/assistant messages 由 Memory service 异步交给 Mem0 ingestion。

Mem0 拥有提取、合并、向量化、索引和持久化。Context/runtime 不实现第二套 ranking、promotion、
profile、冲突处理或 memory tool。完整 Memory 契约见 `docs/memory-service-architecture.md`。

## 7. Tool Observation

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

### Durable task

只有可信 worker resume 可以注入校验后的 durable task snapshot。模型只接收当前执行所需的
objective、constraints、plan/step 状态、artifact references、等待状态和剩余预算；lease、secret、
raw Provider response、父会话历史及未登记扩展不得进入 prompt。Durable snapshot 是当前执行状态，
不是 session summary 或长期记忆。

### Realtime task state

Realtime task state 仅在结构化 interaction mode、entry capability 或显式 runtime opt-in 下启用。
它服务于 Gateway 的 interrupt、artifact、progress、TTS/display 和 side-effect 生命周期，不渲染进
Provider prompt。普通请求不能因携带类似 Gateway 的 metadata 而隐式启用。

### Realtime video

后台 observer、共享语义快照和主 LLM 是三个边界。Runtime 只根据可信入口 profile 和结构化
`video_ids` 判断是否向主 LLM 暴露 `live_view_inspect`；不得把镜头能力状态、共享语义快照、
新鲜度、帧、媒体路径、VLM prompt 或 raw response 被动编译进 Provider prompt。主 LLM 只有自主调用
`live_view_inspect` 后，才能通过该次受治理的 Tool observation 消费视觉语义。Agent-Service 在用户
请求到达时以前一刻最新原始帧为边界并冻结目标 sequence；目标尚未进入语义流水线时将其交互式提升。
工具从统一 `SessionVisualSemanticStore` 读取不晚于该边界的 VLM 文本，对处理中目标最多等待 10 秒，
但不得消费请求到达后视频帧的语义。
超时但 observer 尚未明确失败时保持 `pending`，不得把正常识别耗时
误报为 `unavailable` 或 `failed`。实时观察结果必须独立
记账，不并入 conversation、memory 或 task state。完整媒体协议见
`docs/media-agent-service-websocket.md`。

每个选中关键帧调用后台 VLM 前，`VisualContextService` 在固定的 `before_sequence` 边界编译
`VisualContextPack`。启用视觉压缩时，VLM 只接收旧的 revisioned summary、其后未覆盖的最近逐条
`VisualSemanticRecord` 文本以及当前一张 JPEG；历史文本按独立 VLM tokenizer 对实际投影做 token
preflight，不再由 Provider 施加 4,000 字符截断。每次 observation 使用新建的 Provider WebSocket
conversation，成功、失败或不完整响应后都关闭连接，Provider 侧不会隐式携带上一张图片或回复。
target/trigger/hard 与主 Context 共用同一心智模型：target 决定预计把重建请求降到目标所需的最小
最旧连续 prefix，并据剩余空间收紧本轮 summary output budget；`keep_recent_records` 始终保留。
trigger 启动压缩，hard 是最终 Qwen/VLM observation 调用前的拒绝边界。每次成功压缩后都会重建并
重新计数；只要低于 hard 即可继续，即使最近原文或 summary 使结果仍高于 target，也不会为追逐
target 无限压缩。视觉上下文使用独立的 tokenizer、input limit、safety margin、
summary/output/image/instruction reserve 和配置。

视觉压缩只覆盖代码选定的最旧连续 record prefix，并保留配置数量的最近逐条文本。LLM 只返回固定的
语义数组，不接收或回显 record ID；summary coverage 使用有界的 `covered_record_count`、
`covered_through_sequence` 和固定长度 digest。Store 只为当前 raw retention 内记录保存有界的精确
covered-ID membership，因此迟到记录或相同 sequence 的新记录不会被 frontier 误判为已覆盖；raw
eviction 后 digest/frontier 仍可续接。只有压缩成功、代码 coverage 与 revision 校验通过后才原子替换
summary；CAS revision conflict 会按同一 video/as-of 重读 winning summary 并重建一次 pack，再依据新
pack 继续或 hard fail。soft failure 保留旧 summary 和所有原始记录，hard failure 在无法收敛时跳过
本次最终 Qwen/VLM observation。为收敛预算，独立 LLM visual compactor 可以先按现有状态机最多调用
两次；因此 hard 拒绝不表示此前没有 compactor Provider 调用。未启用 compaction 时才使用旧 rolling
summary 的 2,000 字符兼容路径，并把
`visual_context_compaction.status` 记录为 `unavailable`。这两条路径都不改变 observer 的
one-inflight/one-latest-pending 调度。

`VisualContextSummary` 仅用于下一次后台 VLM 的 context projection，不进入主 Agent prompt、
`ConversationStore`、Mem0 或 `visual_memory_search` 索引。`visual_memory_search` 的候选、as-of、排序和
返回状态始终来自原始 `VisualSemanticRecord`；summary 不删除、替换或隐藏这些 raw records。

Session visual history 同样不被动进入 prompt。只有 Runtime 根据同 user/session 语义存储写入可信
`_trusted_visual_memory_available` 后，`visual_memory_search` 才进入 Tool catalog；调用方 metadata
会先被覆盖，exposure 不检查请求文本。模型只拥有 query/time window/search mode，session 与 as-of
由 Runtime/ToolContext 绑定。ASR 已在上游变为普通 final text，不存在语音 embedding prompt 通道。

### Editable owner context

Owner context 默认关闭，只能由进程配置和可信 owner identity 启用。Loader 必须执行 root containment、
文件类型、symlink、编码、容量和敏感内容校验；非法新版本只能使用同 owner 分区的 last-known-good
或省略。Owner persona 可以影响表达，不能改变工具权限、identity、memory policy 或 provider mode。

### Cross-agent delegation

子 Agent 只接收显式 `context_refs`、子任务预算和脱敏审计摘要。父 conversation、memory context、
raw tool results、Provider payload、secret 和任意未列入 allowlist 的 metadata 不向下传递。
Delegation context 不取代子运行自己的 `AssistantContextPack`。完整路由契约见
`docs/agent-communication-routing.md`。

## 9. Budget、失败与可观测性

Budget 必须按完整 compiled request 计算，包括 messages、tools、tool choice 和 response format；
字符估算只能用于预编译报告，不能冒充 tokenizer 计数。Provider 私有 chat template 的差异由
safety margin 和调用后的 usage 误差观测吸收。

容量治理优先保持因果完整性和证据：

- ContextBuilder 不得用全局字符上限静默裁剪 conversation、memory 或 tool observations。
- Conversation 使用 rolling compaction；tool observation 和专项 context 使用各自的安全投影与局部上限。
- 如果不可压缩的 system、memory、tool schema、durable state 或当前 run 证据仍超过 hard limit，
  Runtime 返回稳定错误，不发送已知超限的 Provider 请求。

`ContextReport` 是脱敏的调试/审计摘要，不是 prompt replay。它可以报告 section accounting、
selected tools、source counts、compaction 状态和 tokenizer preflight，但不得返回原始 prompt、
memory 文本、完整 tool observation、raw Provider payload 或 secret。Token 不可用时必须明确标记
为 unavailable，不能用零伪装。

Canonical `context.build.started` / `context.build.finished` 记录 context 编译生命周期；
最终 compiled `ChatRequest` 只归属对应的 `llm.chat` generation input。查询和脱敏规则见
`docs/observability-harness.md`。

## 10. 验证与权威导航

`tests/core/integration/test_context_lifecycle.py` 保护已登记的 context core invariant，包括预算、
compaction 失败分级、compiled accounting 和 native tool call/result 配对。更具体的 feature 检查
位于 `tests/tdd/**` 或 `evals/system/incubating/**`；tokenizer 误差和摘要语义质量属于 system/Agent eval，
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
