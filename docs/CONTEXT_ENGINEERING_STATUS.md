# Context Engineering Status

Last updated: 2026-07-24

本文件记录上下文工程的当前进展、已实现能力、限制和下一步方向。涉及 assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 的任务，应先读本文件顶部快速交接，再读对应小节、源码和测试。

## 新对话快速交接

如果新对话涉及上下文工程，先读本节即可快速接上当前状态。

- 当前结论：上下文工程第一版已经可用并适合阶段性收口，不是缺核心组件的状态。
- 当前权威入口：本文件。
- 说明：已移除完成态阶段计划，当前以本文件作为上下文工程状态与交接入口。
- 已实现核心闭环：`AssistantContextPack`、`ContextSection v1`、默认关闭的 local owner `SOUL.md` source、Context Compiler v1 redacted report、完整 conversation transcript、仅用于运行时会话治理的 realtime task-state snapshot、独立 `realtime_video_context`、durable task-state snapshot、reusable task artifacts、side-effect records、realtime call-state snapshot、token 报告、trace/API 上下文摘要、skill-style capability catalog 和 repo-local `skills/<skill_id>/SKILL.md` capability loader。session summary、token-aware recent transcript、规则触发压缩、字符预算裁剪和 provider overflow 压缩重试的实现保留但不接入 AgentRuntime。
- AgentRuntime 的 session summary 自动生成与上下文压缩已取消；运行时不做容量压缩、窗口选择或字符预算裁剪，
  已保存的会话轮次全部以 provider-native 原始消息进入 LLM。compactor 类、配置解析和独立 factory
  实现仍保留，但 `MULTIMODAL_AGENT_CONTEXT_COMPACTOR` 不再接入 `AgentGraphRuntime`，构造函数显式传入
  compactor 也不会启用压缩。
- 预算现状：字符/token 预算只报告、不裁剪；AgentRuntime 不执行 token-aware recent transcript、
  字符预算裁剪或 structured summary。
- memory 边界：长期记忆只来自 Mem0。session 创建时按可信身份调用一次 Mem0 `get_all` 并冻结
  snapshot；包括第一轮在内的所有 turn 只复用 snapshot，不再召回。成功 turn 在回复提交后把
  user/assistant messages 异步交给 Mem0 原生 `add`，由 Mem0 负责提取、合并、向量化和持久化。
- session memory snapshot 只保存从 Mem0 原始记录提取的结构化 `LongTermMemory`。不存在 memory read/write
  policy、ranking、profile、promotion 或 memory tool。ContextBuilder 在每轮把同一份冻结 items
  的原始文本按顺序直接组装为历史证据，进入当前 `user` message，不进入 `system` message。
- realtime video 交接：Agent-Service 后台 Qwen observer 对每个 `video_id` 复用一个 persistent WebSocket 并预热 rolling 语义；VLM 使用独立视觉角色模板 prompt，只产出结构化视觉事实，不复用主 LLM 系统提示。AgentRuntime 主 LLM 只知道统一的 `vision_understanding` ToolSpec，图片和视频由工具内部按媒体输入分支，不包含 VLM 观察流程、OCR/品牌/序列图等视觉分析提示词，也不看到帧、JPEG 路径、base64、VLM prompt 或 provider raw response。
- 当前不建议继续做：场景分类器、质量反馈自动调参、组件注册器、裁剪 undo 日志、默认 LLM 摘要、全局 token 强控制。
- 如果用户问“继续上下文工程”：优先做验收案例、调试说明、具体失败复现和小回归测试；不要默认新增复杂架构。
- 按需补读：解释机制时读本文件对应小节；涉及长期记忆写入/检索时读 `docs/memory-service-architecture.md`。

## Current Stage

上下文工程已进入可用实现和硬化阶段，不是单纯规划。

- 多阶段 Context Engine + Memory Policy 计划已经完成；后续应把 `docs/CONTEXT_ENGINEERING_STATUS.md` 作为当前入口。
- 主运行时是 LangGraph/ReAct assistant loop，默认 mock/local/offline。
- `AssistantContextPack` 已接入 assistant 每轮决策，统一收集 request、conversation、memory、realtime video、plan state、tool observations、tool specs、source counts 和 budget。
- `AgentGraphRuntime` 可在 run 入口通过 `ContextSourceCoordinator` 加载一次显式 owner-bound 的 `SOUL.md`，把验证后的 `ContextSourceResult` 冻结到 `AgentState`；同一 run 的多次 assistant iteration 不重复读文件，下一 run 才观察合法更新。
- 生产 provider-native `ChatRequest` 统一通过无副作用 `PromptCompiler` 编译；真实与 mock provider 共用 LangGraph assistant loop。工具预算耗尽后的 finishing turn 仍使用同一通用 system prompt 和 native context，只把工具集合置空。legacy prompt-json renderer 仍只用于离线兼容与测试。
- 通用 system prompt 在每次编译时把带时区的可信本地运行时间放在第一段，确保 Provider 原始 input
  和受长度限制的开发预览都优先显示；当前日期、星期、时间和相对日期解析以该事实为准，不依赖模型
  训练时知识猜测。该事实不承担天气、新闻等外部动态信息查询，外部事实仍必须使用已暴露工具。
- `AssistantContextPack` 会按已选 prompt tools 注入一个小型 skill-style capability catalog；它可从 repo-local `skills/<skill_id>/SKILL.md` 加载 prompt-safe descriptor，并可基于当前请求文本做确定性 descriptor 召回，但只描述何时使用现有受治理工具，不是新的执行路径，也不会读取 `.codex/skills`。
- Context Compiler v1 以 `ContextReport` 暴露每次 LLM call 的 redacted section accounting：`system_prompt`、`request`、`session_summary`、`recent_transcript`、`memory`、`realtime_video_context`、`durable_task_state`、`plan_state`、`tool_observations`、`tool_schema` 和 `tool_capability`，并以非累加的 `context_source_report_v1` 报告 section kind/authority/stability 字符数、稳定 issue code、last-known-good 和版本变化计数；不暴露 SOUL 原文、source version、绝对路径、完整 prompt、memory 文本、视频摘要、tool observation 或 provider payload。兼容 schema 仍保留 `realtime_task_state` section，但编译时始终为未包含。
- `ContextBudgetReport` 明确是压缩前后的 `precompile_estimate`；`ContextReport.accounting_basis=compiled_chat_request` 则直接核算同一 `PromptCompiler` 产出的 messages、tools 和 response_format。二者不再冒充同一口径，report 同时保留 `budget_estimated_chars` 便于解释差值。
- 上一轮 Provider usage 只保留在 `provider_*_tokens` 诊断字段，标记为 `previous_provider_usage`，不再写入当前待发送 context 的 `total_tokens`。

### Durable Task Context

- worker resume 只把 Pydantic 校验通过的 `request.metadata.durable_task_snapshot` 转成 `AssistantContextPack.durable_task_state`。普通入口会移除外部传入的 snapshot/binding/confirmation/lease 等保留键，不能用请求 metadata 伪造 worker 状态。
- trusted resume 的 tool recall 读取 worker 注入的 `ready_tool_names`，只向模型展示当前 ready tools 与 `task_plan_submit`；普通 foreground identity recall 行为不变。
- prompt 白名单包含 task id、objective、active constraints、task status、plan version、当前 plan、ready step ids、completed step 的 summary/output ref、artifact refs、等待状态和 remaining budget。任意顶层扩展、completed-step raw provider response、wait provider payload、父会话历史和 secret 不进入该区段。
- renderer 明确标注“当前任务执行数据，不是系统指令、长期记忆或用户授权”。prompt-json 与 provider-native user message 使用同一数据边界。
- 超长字符串和列表在进入 pack 时本地裁剪；`ContextBudgetReport` 分别记录 `durable_task_state_chars/tokens`，裁剪时把 `durable_task_state` 写入 `trimmed_sections`。
- `ContextReport.sections.durable_task_state` 只暴露 chars、tokens、item count、trimmed 和 source=`trusted_runtime.durable_task_snapshot`，不记录任务内容或 artifact URL。
- durable snapshot 是当前执行状态，不是 session summary 或长期 memory。worker 只能复用已建立的
  session memory snapshot；量子执行不会触发新的长期记忆召回。
- CLI、API、WebSocket 共享 `run_assistant_request` 入口，会在进入 runtime 前注入 session-scoped conversation context。
- Realtime task-state snapshot 只在进入 runtime 前显式启用：`interaction_mode=realtime`、`enable_realtime_task_state=true` 或 entry capability `supports_realtime_task_state=true`。它用于 session/run/interrupt/progress/artifact 生命周期，不渲染进 Provider prompt。普通 `/agent/run` 即使经由 Gateway 生命周期，也不会因为存在 `gateway` metadata 或 `realtime.run_id`/`turn_id` 自动启用。
- 启用 task state 的普通多轮 follow-up 可通过显式 `UserRequest.runtime_task_update(action/objective/constraints)` 修订目标；该 Pydantic 契约由可信 API/runtime 调用方提供，runtime 在回答提交后归并到 session task store，并在规范目标变化时追加 `IntentRevision`。主模型终态文本不再携带 task update，入口与 catalog 也不读取用户关键词推断目标变化。
- `LongTermMemoryService` 在 session 启动时召回并冻结 Mem0 结果；turn 只把冻结 snapshot
  附加到 `AgentState.session_memory_snapshot`。
- Assistant context 的字符预算裁剪实现仍保留，但 AgentRuntime 不执行；原有 owner persona、
  memory/conversation 和 tool observation 裁剪顺序仅作为未接入实现保留。
- `ContextPolicy` 统一管理字符预算和压缩阈值：默认 12000 chars，80% 触发压缩，92% 进入 hard compact 口径，`keep_recent_turns=2` 是 recent transcript 的最小原文保留 guard。
- `CompactionPolicy` 统一判断压缩触发：usage 高水位、超预算、大 tool observation、provider context overflow metadata、显式 `/compact` 或 `compact_context=True`。
- `ContextCompactor`、`DeterministicContextCompactor`、`LLMCompactor` 和 factory 实现均保留，
  但不再装配进 `AgentGraphRuntime`；配置值与构造函数注入都不能重新启用运行时压缩。
- 真实 provider 返回 context overflow 类错误时，assistant loop 会标准化为 `provider_context_overflow` 并停止，不做压缩重试。
- Context budget 保留压缩阶段和原因字段以兼容既有 trace/API schema；AgentRuntime 正常运行时不触发压缩。
- `TokenBudgetReporter` 已作为可选报告层接入，仅报告，不据此选择窗口或裁剪。
- AgentRuntime 中 tool observation 只移除 secret、raw provider/file/media payload 和 inline media
  data URI，不限制文本长度、列表条数、嵌套深度或命令输出长度；原有 observation compaction
  实现保留但不接入运行时。原始 observation 始终不被修改。
- Cross-agent delegation now has a separate child-context boundary in `AgentCommunicationService`: child runs receive explicit `context_refs`, child budget metadata, and redacted audit summaries, not parent history, `memory_context_*`, raw provider payloads, secrets, or raw tool results.
- Trace/API 已暴露 versioned context debug summary，包括 context budget、source counts、tool catalog summary 和 observation compaction summary。
- 离线 Improvement Lab 可把脱敏 trajectory 与显式结构化 eval/test 失败转换为 evidence，确定性聚类后生成 skill/runtime/code 人工评审候选；它不进入 `AgentGraphRuntime`，不放宽 context/trace redaction，也不自动修改 skill、runtime 或代码。
- `/runs/{run_id}/context` 与 `/traces/{trace_id}/context` 返回最新 `context_report_v1`；旧 trace 若只有 `context.budget/source_counts/tool_catalog`，会降级生成兼容 report。
- Context build now also emits canonical `context.build.started` and
  `context.build.finished` trace events with redacted budget, source-count,
  compaction, and tool-catalog summaries.

## Implemented

### Conversation Context

- 会话历史有独立 `ConversationStore` 边界，支持 in-memory 和 JSONL。
- `ConversationStore` 同时保存普通 turn 和 session-scoped `context_summary`；summary 用于当前 session 恢复，不写入长期 memory。
- 默认每个 user/session 不限制历史轮数；`MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS`
  设置为正整数时才限制保留轮数。
- prompt 上下文发送全部已保存 turn 原文；AgentRuntime 不使用 token-aware recent transcript selector。
  `keep_recent_turns=2` 仅保留在未接入的压缩实现中。
- recent transcript token budget 的来源顺序是 metadata `conversation_recent_max_tokens` / `conversation_context_recent_max_tokens` override、`context_budget_max_tokens` 的 20%（带小型 min/max clamp）、最后由 `ContextPolicy.max_context_chars` 按本地 chars-per-token 估算。
- 增量滑动窗口摘要、`ContextSummary.handoff_v2`、`/compact` 与 `compact_context=True` 的处理代码仍保留，
  但 AgentRuntime 不生成、读取或注入 session summary。
- 请求注入 conversation 原始轮次和独立 memory context；`reset_conversation=True` 仍清空 turns 和历史遗留的 session summary。
- 压缩元数据包括 `conversation_context_compacted`、`conversation_context_recent_turns`、`conversation_context_compacted_turns`、`conversation_context_token_aware`、`conversation_context_recent_tokens`、`conversation_context_recent_token_budget`。
- `reset_conversation` metadata 可清空当前 session 的短期对话历史。

### Memory Context

- `LongTermMemoryService` 是 runtime 唯一依赖，统一编排召回、冻结 snapshot 和异步写入，
  不拥有记忆算法。
- `SessionMemorySnapshotStore` 在 session 创建时按可信身份加载一次 Mem0 `get_all` 结果并冻结。
- turn 只复用 snapshot；snapshot 缺失或 Mem0 失败时使用空记忆，不在 turn 内懒加载。
- 默认最多加载 5 条结构化 `LongTermMemory`；ContextBuilder 每轮直接组装其原始文本。
- 不存在 local/remote/framework backend 选择、memory tool、关键词召回、二次 ranking、读写策略、
  profile、冲突处理或 promotion。
- 成功回复提交后，`LongTermMemoryService` 把 user/assistant messages 投递到后台队列，通过
  单次 Mem0 `add` 完成 ingestion；不生成项目自定义的 daily/core 双记录。

### Boundary With Memory Service

上下文工程消费 Mem0 session snapshot，但不拥有 memory 行为。

- Mem0 负责提取、合并、向量化、索引、检索和持久化。
- Memory service 从 Mem0 响应中提取 `LongTermMemory` 并冻结为 `SessionMemorySnapshot`；
  Runtime 只调用 service 生命周期方法。
- ContextBuilder 从本轮 `AgentState.session_memory_snapshot` 读取原始文本并组装上下文；
  snapshot 不写入 request metadata，也不由 memory service 渲染。
- Context engineering 负责把 request、conversation、memory context、plan state、tool observations 和 tool specs 组装成 `AssistantContextPack`。
- Context engineering 负责 prompt/native rendering、tool observation compaction、全局 context budget、source counts 和 trace/debug 摘要。
- Context engineering 不应重新实现 Mem0 的提取、ranking、合并或 store 选择。
- Memory service 不应了解 legacy prompt-json/native-tools 渲染、tool observation compaction 或全局 context budget。

### Tool Observation Compaction

- AgentRuntime 只对进入 assistant prompt 的 observation 副本做安全清洗，不修改或容量压缩原始 observation。
- `shopping_search` 字段白名单、列表条数限制、字符串裁剪和命令输出裁剪实现仍保留，但不接入 AgentRuntime。
- raw provider/file/media payload 字段、base64/data URI、HTML/raw body 等高风险内容会从 prompt 副本中移除。
- image/video/file 类结果保留 `output_ref`、`artifact_ref`、`image_ref`、识别摘要、transcript 等 prompt-safe 信息。
- compaction metadata schema 继续保留兼容字段；metadata 不记录原始 payload。

### Cross-Agent Delegation Context

- `AgentCommunicationService` builds a child-safe delegation context after delegation policy accepts a task and before `AgentTransport` dispatches it.
- Child request metadata preserves explicit `context_refs`, `request_origin`, `agent_communication`, `child_context_budget`, and `agent_context`.
- Parent `conversation_history`, `parent_history`, `memory_context_*`, raw provider payloads, base64/media/body fields, secret/token-like fields, arbitrary non-allowlisted metadata, and raw parent `tool_results` are not forwarded.
- Omitted fields are recorded as field-name/reason pairs in `agent_context.omitted_context`; raw parent tool results are reduced to `tool_result_refs` when output references exist.
- This boundary does not replace `AssistantContextPack` assembly and does not move Mem0 session snapshot
  lifecycle out of `LongTermMemoryService`.

### Prompt Rendering

- `render_prompt_json_context` 是历史 prompt-json renderer，保留给 context renderer 测试和离线兼容材料；生产真实 LLM runtime 不再使用它做决策控制面。
- `PromptCompiler` 是生产 provider 请求的唯一提示词编译入口；它只组合已解析 system profile、已构建 `AssistantContextPack`、已有 native calls/observations 和已选 ToolSpec，不读取 memory/store、不访问 ToolRegistry、不调用 Provider，也不写 trace。
- `render_native_tool_context` 用于 provider-native tool calling，避免重复渲染完整 ToolSpec。
- Provider-native 编译将全部已保存原始轮次还原为独立 `user` / `assistant` messages，再以无重复角色标签的原始请求文本追加当前 `user` message。token-aware selector 与 session summary renderer 仍保留给离线兼容和独立测试，但不接入 AgentRuntime。
- native/legacy context 可渲染 prompt-safe capability catalog；实际执行契约仍是 `ToolSpec`，工具调用仍必须通过 `ToolExecutor`。
- Provider-native `ChatRequest.tools` 使用 `AssistantContextPack.prompt_tool_specs` 中已治理的 schema。context builder 同时生成 prompt-safe `RunToolCatalog`；其中 `available_tool_names` 既是模型可见目录，也是 `ActionValidator` 的 run-scoped 执行边界，不再维护重复的 exposed/executable 集合。目录装配只消费 category、toolset、媒体要求、默认启用以及显式 tool/toolset/skill 等结构化事实，不读取 `request.text` 做意图路由；当前 recall 为 identity，并记录 `recall_identity`。治理后明确为空的集合不会回退完整 registry；未来语义召回必须另行设计高召回率与漏召回恢复机制。
- 系统提示词只承载通用 runtime、数据边界和工具治理规则，不写入某个具体工具的选择策略。具体工具的适用场景、禁用场景、输入要求和副作用说明由 `ToolSpec.when_to_use`、`when_not_to_use`、`runtime_constraints` 和 side-effect metadata 随 provider-native tool schema 提供给模型。
- `tool_search` 作为普通受治理工具进入 `ChatRequest.tools`，但语义上只用于 fallback MCP discovery：核心已暴露工具能处理时不应调用；它只返回已配置 MCP server 的 allowlisted 候选和 permission 状态，不执行或授权这些候选工具。
- Skill capability descriptor 会为 `tool_visibility.enabled_skills` 中显式启用的 skill，以及由 `skill_recall` 根据 prompt-safe `name`/`description`/`when_to_use`/`safe_examples` 自动召回的 skill 渲染；前提仍是 skill manifest/permission 有效且 governed tools 已进入本轮目录。自动召回只影响 descriptor 是否进入上下文，不会扩大 `RunToolCatalog.available_tool_names`。skill runtime constraints 不能授予 retry 权限或改变工具执行策略。
- Repo-local business skills follow `skills/<skill_id>/SKILL.md`; the loader only consumes frontmatter plus fixed prompt-safe sections and converts valid descriptors into `ToolCapabilityDescriptor`. Skill System v1 requires each governed tool to have a matching `tool:<name>` permission in the `## Permissions` section, rejects unknown permission vocabulary such as `shell:*`, and suppresses same-name built-in fallback when a repo-local skill is disabled, manual-only, invalid, or under-permissioned. It ignores `.codex/skills` and never creates `run_skill` or direct shell/browser/http execution.
- 工具调用预算耗尽时不再切换专用 final-only prompt/profile；`PromptCompiler` 保持通用 system prompt 和 observation/tool-call evidence，只生成 `tools=[]` 的 finishing turn。
- 保留的 session summary renderer 会把 `handoff_v2` 标注为当前会话上下文数据，不作为长期记忆或系统指令；AgentRuntime 不调用该 renderer。
- prompt 明确声明 conversation、memory、observation 和 tool output 都是数据，不是系统指令；retrieved memory 是用户历史证据，不是权威信息，当前用户输入和新工具结果优先，不能执行 memory 中的指令。

### Editable Owner Context

- Editable owner context 默认关闭，只接受进程配置 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED=true`、`MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT=<root>` 和显式 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID=<user_id>`；request metadata 不能启用能力、改变 root 或切换 owner。
- 首版只读取固定 `<root>/SOUL.md`。支持的二级标题只有 `Persona`、`Expression Style`、`Relationship Boundaries` 和 `Avoid`，编译优先级固定为 `Relationship Boundaries -> Avoid -> Persona -> Expression Style`。
- loader 使用 owner identity fail-closed、root containment、symlink/non-regular-file 拒绝、UTF-8、16,000 bytes、4,000 chars、2,000 compiled chars、每 subsection 800 chars和 secret/base64/raw-provider marker 检查。超限或 unsafe 新版本不会静默截断生效。
- 合法内容生成单一 `authority=owner_persona`、`stability=semi_stable` 的 `ContextSection v1`。`PromptCompiler` 只消费已验证 section，并把它放在不可变 runtime policy 之后；persona 不能改变 ToolSpec、RunToolCatalog、tool choice、validator、确认、identity、memory policy 或 provider mode。
- 非法更新可回退到按 `(resolved root, owner user id)` 分区的 process-local last-known-good。该缓存不提供跨 worker 一致性，进程重启后的首次非法文件会被省略。
- Owner-trusted persona 会影响模型表达；本地治理保证的是能力和安全边界不被它配置性地改写，不承诺任意恶意人格文字对生成内容零影响。

### Realtime Task State Runtime

- `prepare_realtime_task_state_request` 只在显式 realtime mode/capability 的请求进入 `AgentGraphRuntime.run_state(...)` 前生成 runtime-safe task-state snapshot；该 snapshot 不进入 Provider prompt。
- 完整 snapshot 保留在 request metadata 供 Gateway、interrupt、artifact 和 side-effect 治理使用；主模型依赖当前用户请求和 conversation context 承接意图，不接收 task-state snapshot 或其语义 projection。
- Task-state 记录 session 内当前 objective、active constraints、source turn/run ids、interrupt 产生的 `IntentRevision`，以及 completed run 后的 prompt-safe `TaskArtifact`、lightweight checkpoint artifact 和 `SideEffectRecord`。
- Task-state 现在也记录 prompt-safe realtime call state：`pending_tool`、`tts_state`、`last_spoken_progress`、`speech_turn_id`、`barge_in_source` 和 bounded `last_realtime_event_ids`，用于表达工具等待、展示/TTS 状态和打断来源；工具完成/失败、取消和挂断会清理 pending tool，TTS/display started/finished/superseded 会更新展示状态；不保存 raw audio、raw transcript stream 或 provider payload。
- `pending_tool` 会消费 `tool_started` 事件中的 prompt-safe `pre_tool_call` 摘要，保留工具副作用等级、risk gate、idempotency key 摘要和是否需要确认，便于 interrupt 后选择重规划、等待确认、去重或补偿路径。
- Interrupt run 的 snapshot 会保留原始 objective，并把最新 interrupt 文本写入 `latest_revision`；普通 queued follow-up 只更新 current user text 和 provenance，不创建 revision。
- Completed realtime run 会按 `ToolSpec.execution.artifact_reuse` 把 selected tool observations 和 media refs 记录为 task artifacts；tool observation artifact 复用现有 prompt compaction 逻辑，不保存 raw provider/file/media payload。
- 多步 realtime run 在同一轮完成至少两个 reusable tool observations 时，会记录 bounded `checkpoint` artifact；interrupt 只有在 checkpoint 仍可复用时才选择 `resume_from_checkpoint`，用户明确重来/换一批会把 checkpoint 标为 stale。
- Interrupt 会用简单策略选择 `restart`、`reuse_and_replan`、`ask_confirmation`、`report_committed` 或 `compensate`；如果用户明确要求重新搜索/换一批/不要之前结果，已有 reusable artifacts 会标记为 `stale`，不会重新注入 prompt snapshot。
- Side-effect records 来自 `ToolSpec.side_effect` 和工具结果中的 prompt-safe override（例如 `requires_confirmation`、`confirmation_id`、`side_effect_level`）；read-only 工具不阻塞重规划，pending confirmation 会让下一轮先处理确认，committed action 不会被描述成已取消，compensatable artifact 会倾向修正版/补偿路径。
- `AgentGraphRealtimeBackend` 和 shared run service 会发 display-only `run.progress`，用于 App + Media 展示 `task_state/revising`、strategy、reusable artifact count 和 side-effect count。
- Realtime delivery policy 将 `run.progress` 和 tool lifecycle 标记为 `persistence=ephemeral`，只有 `response.chunk` 属于 `persistence=final`；progress 使用 run-scoped replacement key，并由 final chunk 或 `run.end` supersede，因此不会作为 assistant final text 进入 conversation history 或长期 memory。
- 当前已接入 process-local risk gate/idempotency ledger：read-only 工具无额外开销，compensatable 工具可去重，realtime 未分类 hard-gate 工具会返回 pending confirmation。durable task 有独立 SQLite task/confirmation/lease 恢复与身份隔离 API；通用确认 UX 和跨进程幂等 ledger 仍未接入。

### Realtime Video Context

- Agent-Service 的后台 observer 继续通过工具治理链执行 Qwen；“后台受治理工具执行”和“AgentRuntime 可见工具目录”是两个边界。有 active video 的可信 AgentRuntime turn 可以通过动态工具目录暴露 `vision_understanding`，但主 LLM 只看到唯一视觉工具 schema 和 prompt-safe 文本上下文，不见帧、内部媒体路径或 VLM 角色模板。
- `AgentGraphRuntime` 在每次模型 context build 前按请求中最后一个 `video_id` 重新投影共享 `RealtimeVideoMemoryStore`，生成 `ready`、`refreshing`、`pending`、`stale`、`failed` 或 `unavailable` 状态。
- 可信 Agent-Service 入口把 `video_ids` 渲染为“当前共享的实时画面”而不是上传式“附带视频 ID”；AgentRuntime system prompt 保持入口和通道无关，不注入电话、口播、挂断或传输层规则。普通上传/API 仍保留上传语义。
- observer 首帧必选、明显变化立即候选、静态画面最长 2 秒产生一次候选；队列保持一个 Qwen in-flight 和一个 latest-wins pending。每轮 Provider 请求只含当前单帧和最多 2,000 字符的上一成功语义摘要，不重发多帧历史。
- 明确指代当前画面的问题由 AgentRuntime 主 LLM 通过动态暴露的 `vision_understanding` 表达；工具内部选择视频分支，入口不放视觉分析提示词，也不基于文本自行完成 VLM 判断。问候/闲聊不应主动提及视觉。
- 每个 `video_id` 复用一个 persistent Qwen WebSocket；20 次成功观察或 60 秒后轮换，断线按 0.25/0.5/1/2/5 秒封顶退避重连。失败保留最后成功快照并投影 `refreshing`/`stale`；关闭或切换 video id 会关闭 Provider session 并清理 pending、语义状态和帧文件。
- 投影只包含裁剪后的 summary、objects、people、actions、events、scene、`snapshot_sequence`、`target_sequence`、`completed_sequence`、`sequence_gap`、观察耗时、provider/model、`transport`、`session_generation`、`connection_reused`、`reconnect_count` 和 pending/in-flight 状态，序列化上限约 2,000 字符；不含帧路径、媒体数据、Qwen 原文、raw event 或 provider 原始错误。
- `snapshot_age_ms` 与 `frame_capture_age_ms` 以成功语义对应帧的采集时间为主，`snapshot_publish_age_ms` 独立表示结果发布时间年龄；缺失或未来采集时间返回空值，不伪造年龄。存在正 `sequence_gap` 时投影为 `stale`（仍有任务时为 `refreshing`），prompt 不得把旧观察断言成当前事实。
- renderer 把它标注为被动外部观察数据，不作为工具调用策略；何时调用 `vision_understanding` 只由本轮动态 ToolSpec 描述和模型工具选择表达。
- `ContextBudgetReport`、token estimate、`ContextReport.sections.realtime_video_context` 和 `context.build.finished.realtime_video` 独立记账，不并入 conversation、memory、realtime task state 或普通 tool observation。

### Context Budget And Observability

- `ContextBudgetReport` 统计 request、conversation、memory、realtime video、plan、observations、tool specs、`owner_persona_chars` 和 total chars，并报告 `context_usage_ratio`、`compaction_triggered`；启用本地 token 估算时，最终实际注入的 persona 同时计入 `owner_persona_tokens` 和 `total_tokens`。
- `ContextBudgetReport` also tracks `tool_capability_chars` so the skill-style capability catalog is visible in budget/debug output. `AssistantContextPack` and `ContextReport` carry prompt-safe `skill_report_v1` fields for loaded, explicit, auto-candidate, selected, skipped, fallback, override, governed-tool, auto-recall reason, and permission issue visibility.
- Context budget 仍以默认 12000 chars 生成观测报告；即使超过该值也不裁剪。
- 可选 token budget 字段包括 section token estimates、`total_tokens`、`max_tokens`、`token_usage_ratio`、`token_budget_source` 和 provider usage counters；它们只用于报告，不替代 char budget control path。
- 本地 token 估算通过 `context_budget_estimate_tokens=True` 或 `context_budget_max_tokens` 启用；provider usage metadata 如 `context_token_usage` / `provider_token_usage` / `last_chat_usage` 优先于估算。
- assistant loop 会把 `ChatResult.usage` 归一为安全 token counters 写入 request metadata，供下一轮 context budget report 使用；raw provider payload 字段不会写入 metadata/trace。
- 超过预算只记录 `over_budget`；AgentRuntime 不在 prompt 副本中裁剪 memory、conversation 或 observations。
- `compression_stage` 记录 `none`、`compacted` 或 `budget_trimmed`；`compression_reasons` 记录 `conversation_context_compacted`、`observation_context_compacted`、`context_usage_high`、`tool_observation_too_large`、`provider_context_overflow`、`explicit_compact`、`context_over_budget`、`context_budget_trimmed`。
- 预算裁剪优先保留工具 observation，因为它通常是下一步工具调用和最终回答的证据来源。
- assistant decision trace 写入 `context_schema_version="context_observability_v1"`、budget、source counts、compaction summary、tool catalog summary、`compactor_type` 和 `context_summary_present`；compaction summary 只暴露 pruning/truncation 计数，不暴露 raw payload。
- `react.decision` trace 的 `context.report` 事件写入 `context_report_v1`，用于检查真实发送给 provider 的 system prompt 大小、selected native tool schema、memory 注入 ID、realtime task-state 大小和压缩/裁剪状态。
- Context pack construction emits standalone `context.build.started` /
  `context.build.finished` canonical trace events. The finished event carries the
  same redacted context summary shape used by trace/API context debugging，并额外携带
  prompt-safe `context_report_v1` section accounting，供 Langfuse `context.build` output
  直接展示；最终 compiled `ChatRequest` 仍归属对应 `llm.chat` generation input。
- Trace sanitization 会过滤 `raw_provider_payload`、`raw_provider_response`、base64/media/file payload key 和 secret key，作为 public API 前的额外防线。
- `/runs/{run_id}` 与 `/traces/{trace_id}` 可查询 context 相关摘要。

### LLM Compactor And Provider Overflow

- `SummaryValidator` 要求 LLM compactor 输出完整旧 schema，并拒绝 secret/API key/base64/raw provider payload 等不应持久或注入的内容；可选 `handoff_v2` 同样经过递归安全校验，unsafe 或 invalid 输出回退 deterministic。
- LLM compactor prompt 会先移除 `raw_provider_payload`、`raw_payload`、`raw_html` 等高风险字段，再交给 provider。
- Summary 中如引用 `tool_call:` / `tool_call_id:`，必须保留对应 `tool_result:` / `tool_result_id:`，反向亦然，避免压缩时切断工具调用和结果证据链。
- Provider HTTP 413、`context_length_exceeded`、`context_overflow`、`input_too_large` 和 `request_too_large` 会归一为 `provider_context_overflow`。
- Provider context overflow 仍归一为稳定错误，但 AgentRuntime 不压缩或重试。

## Current Limitations

- session structured summary 在 AgentRuntime 中关闭；会话上下文发送全部已保存轮次原文。
  deterministic 与 LLM semantic compactor 实现均保留，但运行时配置和构造函数注入都不会启用。
- AgentRuntime 不执行全局容量压缩；character/token budget 仅用于报告。Mem0 session recall 只受
  `top_k` 限制，ContextBuilder 不再对冻结记忆文本设置额外字符上限。
- Context Compiler v1 是调试/审计摘要，不是 prompt replay。它刻意不返回 raw prompt、raw provider payload、完整 memory 文本或完整 tool observation；token 字段仍依赖现有估算或 provider usage metadata。
- 显式本地 trace-content + loopback OTLP 模式是独立的 prompt 调试例外：assistant loop
  会在 Provider 调用前把最终 compiled `ChatRequest` 暂存到进程内 store，并作为对应
  Langfuse `llm.chat` generation input 导出。该能力不改变 Context Compiler/API 的摘要契约，
  不写 JSONL，不保存 Provider 原始响应或 hidden reasoning。
- Editable owner context 当前只实现本机 owner-bound `SOUL.md`；没有实现 `USER.md` / `MEMORY.md` projection、skill L1/L2 view、Provider cache hint 或跨进程 last-known-good。
- memory extraction、向量化和持久化全部由 Mem0 实现，项目不维护第二套检索算法。
- 保留的会话历史压缩实现只增量合并滑出 token-aware recent window 的较早轮次；AgentRuntime 当前不调用该实现。
- assistant loop 不向主 LLM 暴露任何 memory tool；主 LLM 只消费 session 启动时冻结的 Mem0 snapshot。

## Key Files

- `src/assistant_agent/services/context/builder.py`
- `src/assistant_agent/services/context/conversation.py`
- `src/assistant_agent/services/context/compaction.py`
- `src/assistant_agent/services/context/policy.py`
- `src/assistant_agent/services/context/token_budget.py`
- `src/assistant_agent/services/context/compactor.py`
- `src/assistant_agent/services/context/renderer.py`
- `src/assistant_agent/services/context/report.py`
- `src/assistant_agent/services/context/capability_catalog.py`
- `src/assistant_agent/services/context/skill_loader.py`
- `src/assistant_agent/services/context/sources.py`
- `src/assistant_agent/services/context/soul_source.py`
- `src/assistant_agent/services/realtime_task_state.py`
- `src/assistant_agent/memory/service.py`
- `src/assistant_agent/memory/models.py`
- `src/assistant_agent/memory/session_snapshot.py`
- `src/assistant_agent/services/realtime_video_memory.py`
- `src/assistant_agent/services/realtime_video_observer.py`
- `src/assistant_agent/services/video_context.py`
- `src/assistant_agent/services/durable_tasks/`
- `src/assistant_agent/services/agent_delegation_context.py`
- `src/assistant_agent/schemas/context.py`
- `src/assistant_agent/services/assistant_run_service.py`
- `src/assistant_agent/services/chat_adapter.py`
- `src/assistant_agent/services/provider_errors.py`
- `src/assistant_agent/services/improvement/`
- `src/assistant_agent/schemas/improvement.py`
- `scripts/run_improvement_lab.py`
- `src/assistant_agent/memory/mem0/client.py`
- `src/assistant_agent/memory/ingestion_queue.py`
- `src/assistant_agent/agent/assistant_loop_nodes.py`

## Validation Boundary

The default pytest suite does not mirror context internals. It protects the complete text run, tool loop and
identity isolation only. Context budgeting, compaction and retrieval quality use deterministic evals and runtime
traces; add a pytest regression only after a concrete user-visible failure or stable protocol change.

## Next Steps

- Add one minimal regression test when a concrete context failure appears and the existing safety net cannot detect it.
- Consider broader token-aware control decisions only if recent transcript and reporting token fields show real provider failures that character budgeting cannot prevent.
- Consider semantic summary or embedding retrieval only after local relevance tests show keyword retrieval is insufficient.
