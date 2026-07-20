# Context Engineering Status

Last updated: 2026-07-17

本文件记录上下文工程的当前进展、已实现能力、限制和下一步方向。涉及 assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 的任务，应先读本文件顶部快速交接，再读对应小节、源码和测试。

## 新对话快速交接

如果新对话涉及上下文工程，先读本节即可快速接上当前状态。

- 当前结论：上下文工程第一版已经可用并适合阶段性收口，不是缺核心组件的状态。
- 当前权威入口：本文件。
- 说明：已移除完成态阶段计划，当前以本文件作为上下文工程状态与交接入口。
- 已实现核心闭环：`AssistantContextPack`、`ContextSection v1`、默认关闭的 local owner `SOUL.md` source、Context Compiler v1 redacted report、session summary、token-aware recent transcript、增量滑动窗口摘要、session handoff v2、realtime task-state snapshot、独立 `realtime_video_context`、durable task-state snapshot、reusable task artifacts、side-effect records、realtime call-state snapshot、规则触发压缩、tool observation prompt 副本裁剪、字符预算控制、token 报告、provider overflow retry-once、trace/API 上下文摘要、skill-style capability catalog 和 repo-local `skills/<skill_id>/SKILL.md` capability loader。
- 默认摘要方式：deterministic/local；`LLMCompactor` 只在 `provider_smoke` 或 `pilot` 且非 mock chat adapter 下启用。
- 预算现状：全局压缩控制仍以字符预算为准；recent transcript 选择已使用本地 token 估算；Memory context 有单独 token-aware 注入边界；其余 token 字段仍主要用于报告。
- memory 边界：`context_summary` 是当前 session 状态，不是长期 memory；长期读取由 `MemoryReadPolicy` gate，长期写入仍由 `MemoryManager` / `MemoryWritePolicy` 管。
- realtime video 交接：Agent-Service 后台 Qwen observer 对每个 `video_id` 复用一个 persistent WebSocket 并预热 rolling 语义；VLM 使用独立视觉角色模板 prompt，只产出结构化视觉事实，不复用主 LLM 系统提示。AgentRuntime 主 LLM 只知道在工具目录动态提供 `video_understanding` 时可以调用该工具，不包含 VLM 观察流程、OCR/品牌/序列图等视觉分析提示词，也不看到帧、JPEG 路径、base64、VLM prompt 或 provider raw response。
- 当前不建议继续做：场景分类器、质量反馈自动调参、组件注册器、裁剪 undo 日志、默认 LLM 摘要、全局 token 强控制。
- 如果用户问“继续上下文工程”：优先做验收案例、调试说明、具体失败复现和小回归测试；不要默认新增复杂架构。
- 按需补读：解释机制时读本文件对应小节；涉及长期记忆写入/检索时读 `docs/memory-service-architecture.md`。

## Current Stage

上下文工程已进入可用实现和硬化阶段，不是单纯规划。

- 多阶段 Context Engine + Memory Policy 计划已经完成；后续应把 `docs/CONTEXT_ENGINEERING_STATUS.md` 作为当前入口。
- 主运行时是 LangGraph/ReAct assistant loop，默认 mock/local/offline。
- `AssistantContextPack` 已接入 assistant 每轮决策，统一收集 request、conversation、memory、realtime video、plan state、tool observations、tool specs、source counts 和 budget。
- `AgentGraphRuntime` 可在 run 入口通过 `ContextSourceCoordinator` 加载一次显式 owner-bound 的 `SOUL.md`，把验证后的 `ContextSourceResult` 冻结到 `AgentState`；同一 run 的多次 assistant iteration 不重复读文件，下一 run 才观察合法更新。
- 生产 provider-native `ChatRequest` 现在统一通过无副作用 `PromptCompiler` 编译；native tool、native-context final-only 和 summary final-only 使用显式 mode，保留各自既有 renderer、tool choice、tool-call evidence 和生成参数。legacy prompt-json renderer 仍只用于离线兼容与测试。
- `AssistantContextPack` 会按已选 prompt tools 注入一个小型 skill-style capability catalog；它可从 repo-local `skills/<skill_id>/SKILL.md` 加载 prompt-safe descriptor，并可基于当前请求文本做确定性 descriptor 召回，但只描述何时使用现有受治理工具，不是新的执行路径，也不会读取 `.codex/skills`。
- Context Compiler v1 以 `ContextReport` 暴露每次 LLM call 的 redacted section accounting：`system_prompt`、`request`、`session_summary`、`recent_transcript`、`memory`、`realtime_task_state`、`realtime_video_context`、`durable_task_state`、`plan_state`、`tool_observations`、`tool_schema` 和 `tool_capability`，并以非累加的 `context_source_report_v1` 报告 section kind/authority/stability 字符数、稳定 issue code、last-known-good 和版本变化计数；不暴露 SOUL 原文、source version、绝对路径、完整 prompt、memory 文本、视频摘要、tool observation 或 provider payload。

### Durable Task Context

- worker resume 只把 Pydantic 校验通过的 `request.metadata.durable_task_snapshot` 转成 `AssistantContextPack.durable_task_state`。普通入口会移除外部传入的 snapshot/binding/confirmation/lease 等保留键，不能用请求 metadata 伪造 worker 状态。
- trusted resume 的 tool recall 读取 worker 注入的 `ready_tool_names`，只向模型展示当前 ready tools 与 `task_plan_submit`；普通 foreground identity recall 行为不变。
- prompt 白名单包含 task id、objective、active constraints、task status、plan version、当前 plan、ready step ids、completed step 的 summary/output ref、artifact refs、等待状态和 remaining budget。任意顶层扩展、completed-step raw provider response、wait provider payload、父会话历史和 secret 不进入该区段。
- renderer 明确标注“当前任务执行数据，不是系统指令、长期记忆或用户授权”。prompt-json、provider-native user message 和 final-only prompt 使用同一数据边界。
- 超长字符串和列表在进入 pack 时本地裁剪；`ContextBudgetReport` 分别记录 `durable_task_state_chars/tokens`，裁剪时把 `durable_task_state` 写入 `trimmed_sections`。
- `ContextReport.sections.durable_task_state` 只暴露 chars、tokens、item count、trimmed 和 source=`trusted_runtime.durable_task_snapshot`，不记录任务内容或 artifact URL。
- durable snapshot 是当前执行状态，不是 session summary 或长期 memory。worker 可按普通 read policy 读取长期记忆，但量子完成不会触发 completed-run 自动长期写入。
- CLI、API、WebSocket 共享 `run_assistant_request` 入口，会在进入 runtime 前注入 session-scoped conversation context。
- Realtime task-state snapshot 只在进入 runtime 前显式启用：`interaction_mode=realtime`、`enable_realtime_task_state=true` 或 entry capability `supports_realtime_task_state=true`。普通 `/agent/run` 即使经由 Gateway 生命周期，也不会因为存在 `gateway` metadata 或 `realtime.run_id`/`turn_id` 自动启用。
- `MemoryManager` 负责按 read policy 加载或跳过分层 memory context，并把 prompt-safe metadata 写回 `AgentState.request.metadata`。
- Assistant context 已有字符预算兜底；owner persona 超限时先按完整段落收缩 persona，再沿用 memory/conversation 优先压缩、工具 observation 最后压缩的顺序。
- `ContextPolicy` 统一管理字符预算和压缩阈值：默认 12000 chars，80% 触发压缩，92% 进入 hard compact 口径，`keep_recent_turns=2` 是 recent transcript 的最小原文保留 guard。
- `CompactionPolicy` 统一判断压缩触发：usage 高水位、超预算、大 tool observation、provider context overflow metadata、显式 `/compact` 或 `compact_context=True`。
- `ContextCompactor` 已抽象为可插拔边界；默认 deterministic/local，不调用真实 LLM。`LLMCompactor` 仅在 `provider_smoke` 或 `pilot` profile 且 chat adapter 非 mock 时启用，输出无效时回退 deterministic。
- 真实 provider 返回 context overflow 类错误时，assistant loop 会标准化为 `provider_context_overflow`，触发 hard compaction 后重试一次，仍失败则停止并返回可解释最终回答。
- Context budget 会报告自动压缩阶段和原因，便于 trace/API 判断是否发生 conversation、observation 或 budget 级压缩。
- `TokenBudgetReporter` 已作为可选报告层接入；recent transcript selector 复用本地 token 估算；默认全局压缩触发仍使用字符预算，metadata 启用估算或提供 provider usage 时才填充全局 token fields。
- Memory context now has a separate `MemoryContextBuilder` token-aware injection boundary. It can enforce memory-only token budgets and report injected IDs, token count, omitted count, rejection reasons, and retrieval version without changing global assistant context compaction.
- Tool observation compaction 会在 prompt 副本中移除 raw provider/file/media payload、inline media data URI 和过大的命令输出；原始 observation 不被修改。
- Cross-agent delegation now has a separate child-context boundary in `AgentCommunicationService`: child runs receive explicit `context_refs`, child budget metadata, and redacted audit summaries, not parent history, `memory_context_*`, raw provider payloads, secrets, or raw tool results.
- Trace/API 已暴露 versioned context debug summary，包括 context budget、source counts、tool catalog summary、observation compaction summary 和 memory promotion counters。
- 离线 Improvement Lab 可把脱敏 trajectory 与显式结构化 eval/test 失败转换为 evidence，确定性聚类后生成 skill/runtime/code 人工评审候选；它不进入 `AgentGraphRuntime`，不放宽 context/trace redaction，也不自动修改 skill、runtime 或代码。
- `/runs/{run_id}/context` 与 `/traces/{trace_id}/context` 返回最新 `context_report_v1`；旧 trace 若只有 `context.budget/source_counts/tool_catalog`，会降级生成兼容 report。
- Context build now also emits canonical `context.build.started` and
  `context.build.finished` trace events with redacted budget, source-count,
  compaction, and tool-catalog summaries.

## Implemented

### Conversation Context

- 会话历史有独立 `ConversationStore` 边界，支持 in-memory 和 JSONL。
- `ConversationStore` 同时保存普通 turn 和 session-scoped `context_summary`；summary 用于当前 session 恢复，不写入长期 memory。
- 默认每个 user/session 保留最近 8 轮历史，配置项是 `MULTIMODAL_AGENT_MAX_CONVERSATION_HISTORY_TURNS`。
- prompt 上下文使用 token-aware recent transcript selector：从最新 turn 向前估算渲染 token，短 turn 可保留超过 2 轮原文；`keep_recent_turns=2` 仍作为最小原文保留 guard，至少保留最新 turn。
- recent transcript token budget 的来源顺序是 metadata `conversation_recent_max_tokens` / `conversation_context_recent_max_tokens` override、`context_budget_max_tokens` 的 20%（带小型 min/max clamp）、最后由 `ContextPolicy.max_context_chars` 按本地 chars-per-token 估算。
- 会话摘要采用增量滑动窗口：每轮进入 runtime 前，只把新滑出 token-aware recent window 且尚未在 summary refs 中出现的 turn 合并进 `context_summary`，避免重复压缩同一 turn；`/compact` / `compact_context=True` 会强制退回最小 recent guard，但不会重摘要 raw recent turns。
- `ContextSummary` 仍是当前 session 状态，并额外包含可选 `handoff_v2`：`objective`、`active_constraints`、`completed`、`in_progress`、`blocked`、`next_steps`、`evidence_refs`。该字段是 additive schema，不替代旧 summary fields。
- 请求注入顺序是 session summary、recent turns、memory context；`reset_conversation=True` 同时清空 turns 和 session summary。
- 压缩元数据包括 `conversation_context_compacted`、`conversation_context_recent_turns`、`conversation_context_compacted_turns`、`conversation_context_token_aware`、`conversation_context_recent_tokens`、`conversation_context_recent_token_budget`。
- `reset_conversation` metadata 可清空当前 session 的短期对话历史。

### Memory Context

- `MemoryManager` 是 memory 检索、上下文格式化、显式保存、去重、用户画像更新和 completed-run promotion candidate 的边界。
- 自动 memory context 注入先走 `MemoryReadPolicy`。普通首次文案、建议、搜索、生成或推荐不自动查长期记忆；明确提到上次、之前、已保存记忆、个人偏好、继续旧任务，或明显是个人风格/偏好定制请求时才查。个人风格/偏好定制触发口径很窄，例如包含 `风格`/`偏好`/`喜好`/`口味` 且同时包含 `推荐`/`方案`/`文案`/`设计`/`搭配`/`回答`/`写`/`生成`/`继续`。
- memory context 分层为 semantic、session、episodic、artifact、procedural。
- 默认 `top_k=5`，默认 `max_context_chars=500`。
- `MemoryContextBuilder` 负责实际注入选择；`MemoryContext.items` 表示已注入的 memory 子集，而不是所有检索候选。
- 可通过 `memory_context_max_tokens` / `memory_context_budget_tokens` 或 `MemoryManager` 参数限制 memory context token budget。
- memory context metadata includes `memory_context_tokens`, `memory_context_budget_tokens`, `memory_context_omitted_count`, `memory_context_rejected_reasons`, `memory_context_retrieval_version`, `memory_context_injected_ids`, `memory_context_skipped`, `memory_context_policy_reason`, `memory_read_policy`, and `memory_trust_policy`.
- 非空 query 走关键词/中文片段相关性门控；只有明确承接型 query 才允许 recent memory fallback。
- 显式用户记忆会合并重复项，并更新 compact `user_profile` 记忆。
- completed-run summary 默认只生成 policy-gated promotion candidate 和审计 metadata，不自动写长期 memory；`allow_auto_write=True` 时才会落库。

### Boundary With Memory Service

上下文工程消费 memory service 产出的 prompt-safe memory context，但不拥有 memory 行为。

- Memory service 负责 memory item 的存储、检索、排序、分层、写入、去重、用户画像、TTL、审计和删除。
- `MemoryManager` 可以把检索结果格式化为 `MemoryContext`，并写入 `request.metadata["memory_context_*"]`。
- Context engineering 负责把 request、conversation、memory context、plan state、tool observations 和 tool specs 组装成 `AssistantContextPack`。
- Context engineering 负责 prompt/native rendering、tool observation compaction、全局 context budget、source counts 和 trace/debug 摘要。
- Context engineering 不应重新实现 memory 检索、ranking、fallback、write policy、profile merge 或 store 选择。
- Memory service 不应了解 legacy prompt-json/native-tools 渲染、tool observation compaction 或全局 context budget。

### Tool Observation Compaction

- 工具 observation 在进入 assistant prompt 前会压缩，不修改原始 observation。
- `shopping_search`、`product_search` 和 `price_compare` 有专门字段白名单，只保留标题、价格、URL、平台、可用性、评分、相似度等决策必要字段。
- 列表默认最多保留 3 条，超出记录 omitted count。
- 字符串默认最多保留 1200 字，超出添加 truncated 标记。
- raw provider/file/media payload 字段、base64/data URI、HTML/raw body 等高风险内容会从 prompt 副本中移除。
- image/video/file 类结果保留 `output_ref`、`artifact_ref`、`image_ref`、识别摘要、transcript 等 prompt-safe 信息。
- command stdout/stderr/log 类输出默认最多保留 20 行和 1200 字符。
- compaction metadata 记录 original chars、compacted chars、max items、max text chars、被剪掉的 key 名和命令输出裁剪限制；metadata 不记录原始 payload。

### Cross-Agent Delegation Context

- `AgentCommunicationService` builds a child-safe delegation context after delegation policy accepts a task and before `AgentTransport` dispatches it.
- Child request metadata preserves explicit `context_refs`, `request_origin`, `agent_communication`, `child_context_budget`, and `agent_context`.
- Parent `conversation_history`, `parent_history`, `memory_context_*`, raw provider payloads, base64/media/body fields, secret/token-like fields, arbitrary non-allowlisted metadata, and raw parent `tool_results` are not forwarded.
- Omitted fields are recorded as field-name/reason pairs in `agent_context.omitted_context`; raw parent tool results are reduced to `tool_result_refs` when output references exist.
- This boundary does not replace `AssistantContextPack` assembly and does not move memory retrieval, ranking, write policy, or store access out of `MemoryManager`.

### Prompt Rendering

- `render_prompt_json_context` 是历史 prompt-json renderer，保留给 context renderer 测试和离线兼容材料；生产真实 LLM runtime 不再使用它做决策控制面。
- `PromptCompiler` 是生产 provider 请求的唯一提示词编译入口；它只组合已解析 system profile、已构建 `AssistantContextPack`、已有 native calls/observations 和已选 ToolSpec，不读取 memory/store、不访问 ToolRegistry、不调用 Provider，也不写 trace。
- `render_native_tool_context` 用于 provider-native tool calling，避免重复渲染完整 ToolSpec。
- native/legacy context 可渲染 prompt-safe capability catalog；实际执行契约仍是 `ToolSpec`，工具调用仍必须通过 `ToolExecutor`。
- Provider-native `ChatRequest.tools` 使用 `AssistantContextPack.prompt_tool_specs` 中已治理的 schema。context builder 同时生成 prompt-safe `RunToolSet`，记录 registered、qualified、exposed、executable 工具及排除原因，并写入 `AgentState` 供后续 `ActionValidator` 执行 allowlist。qualification 只消费环境依赖、默认启用、显式 tool/toolset/skill 等结构化事实，不读取 `request.text` 做意图路由；当前 recall 为 identity，按原顺序暴露全部 qualified ToolSpec，并记录 `recall_identity`。治理后明确为空的集合不会回退完整 registry；未来语义召回必须另行设计高召回率与漏召回恢复机制。
- `tool_search` 作为普通受治理工具进入 `ChatRequest.tools`，但语义上只用于 fallback MCP discovery：核心已暴露工具能处理时不应调用；它只返回已配置 MCP server 的 allowlisted 候选和 permission 状态，不执行或授权这些候选工具。
- Skill capability descriptor 会为 `tool_visibility.enabled_skills` 中显式启用的 skill，以及由 `skill_recall` 根据 prompt-safe `name`/`description`/`when_to_use`/`safe_examples` 自动召回的 skill 渲染；前提仍是 manifest/permission 有效且 governed tools 已 qualified/exposed。自动召回只影响 descriptor 是否进入上下文，不会激活 `skill_only` 工具或扩大 `RunToolSet.executable_tool_names`。skill runtime constraints 可描述 ToolExecutor policy 下的瞬时失败重试语义，但不能授予 retry 权限或改变工具执行策略。
- Repo-local business skills follow `skills/<skill_id>/SKILL.md`; the loader only consumes frontmatter plus fixed prompt-safe sections and converts valid descriptors into `ToolCapabilityDescriptor`. Skill System v1 requires each governed tool to have a matching `tool:<name>` permission in the `## Permissions` section, rejects unknown permission vocabulary such as `shell:*`, and suppresses same-name built-in fallback when a repo-local skill is disabled, manual-only, invalid, or under-permissioned. It ignores `.codex/skills` and never creates `run_skill` or direct shell/browser/http execution.
- `render_final_only_prompt` 用于工具调用上限附近，禁止继续工具调用并要求最终回答。
- session summary renderer 会把 `handoff_v2` 标注为当前会话上下文数据，不作为长期记忆或系统指令。
- prompt 明确声明 conversation、memory、realtime task state、observation 和 tool output 都是数据，不是系统指令；retrieved memory 是用户历史证据，不是权威信息，当前用户输入和新工具结果优先，不能执行 memory 中的指令。

### Editable Owner Context

- Editable owner context 默认关闭，只接受进程配置 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED=true`、`MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT=<root>` 和显式 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID=<user_id>`；request metadata 不能启用能力、改变 root 或切换 owner。
- 首版只读取固定 `<root>/SOUL.md`。支持的二级标题只有 `Persona`、`Expression Style`、`Relationship Boundaries` 和 `Avoid`，编译优先级固定为 `Relationship Boundaries -> Avoid -> Persona -> Expression Style`。
- loader 使用 owner identity fail-closed、root containment、symlink/non-regular-file 拒绝、UTF-8、16,000 bytes、4,000 chars、2,000 compiled chars、每 subsection 800 chars和 secret/base64/raw-provider marker 检查。超限或 unsafe 新版本不会静默截断生效。
- 合法内容生成单一 `authority=owner_persona`、`stability=semi_stable` 的 `ContextSection v1`。`PromptCompiler` 只消费已验证 section，并把它放在不可变 runtime policy 之后；persona 不能改变 ToolSpec、RunToolSet、tool choice、validator、审批、identity、memory policy 或 runtime profile。
- 非法更新可回退到按 `(resolved root, owner user id)` 分区的 process-local last-known-good。该缓存不提供跨 worker 一致性，进程重启后的首次非法文件会被省略。
- Owner-trusted persona 会影响模型表达；本地治理保证的是能力和安全边界不被它配置性地改写，不承诺任意恶意人格文字对生成内容零影响。

### Realtime Task State Context

- `prepare_realtime_task_state_request` 只在显式 realtime mode/capability 的请求进入 `AgentGraphRuntime.run_state(...)` 前生成 prompt-safe task-state snapshot。
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

- Agent-Service 的后台 observer 继续通过工具治理链执行 Qwen；“后台受治理工具执行”和“AgentRuntime 可见工具目录”是两个边界。有 active video 的可信 AgentRuntime turn 可以通过动态工具目录暴露 `video_understanding`，但主 LLM 只看到工具 schema 和 prompt-safe 文本上下文，不见帧、内部媒体路径或 VLM 角色模板。
- `AgentGraphRuntime` 在每次模型 context build 前按请求中最后一个 `video_id` 重新投影共享 `RealtimeVideoMemoryStore`，生成 `ready`、`refreshing`、`pending`、`stale`、`failed` 或 `unavailable` 状态。
- 可信 Agent-Service 入口把 `video_ids` 渲染为“当前通话的实时镜头”而不是上传式“附带视频 ID”；`realtime_phone` prompt 只保留共享镜头措辞边界，禁止“你刚发送的视频”、视频 ID、快照或内部实现等说法。普通上传/API 仍保留上传语义。
- observer 首帧必选、明显变化立即候选、静态画面最长 2 秒产生一次候选；队列保持一个 Qwen in-flight 和一个 latest-wins pending。每轮 Provider 请求只含当前单帧和最多 2,000 字符的上一成功语义摘要，不重发多帧历史。
- 明确指代当前画面的问题由 AgentRuntime 主 LLM 通过动态暴露的 `video_understanding` 表达；入口不放视觉分析提示词，也不基于文本自行完成 VLM 判断。问候/闲聊不应主动提及视觉。
- 每个 `video_id` 复用一个 persistent Qwen WebSocket；20 次成功观察或 60 秒后轮换，断线按 0.25/0.5/1/2/5 秒封顶退避重连。失败保留最后成功快照并投影 `refreshing`/`stale`；关闭或切换 video id 会关闭 Provider session 并清理 pending、语义状态和帧文件。
- 投影只包含裁剪后的 summary、objects、people、actions、events、scene、`snapshot_sequence`、`target_sequence`、`completed_sequence`、`sequence_gap`、观察耗时、provider/model、`transport`、`session_generation`、`connection_reused`、`reconnect_count` 和 pending/in-flight 状态，序列化上限约 2,000 字符；不含帧路径、媒体数据、Qwen 原文、raw event 或 provider 原始错误。
- `snapshot_age_ms` 与 `frame_capture_age_ms` 以成功语义对应帧的采集时间为主，`snapshot_publish_age_ms` 独立表示结果发布时间年龄；缺失或未来采集时间返回空值，不伪造年龄。存在正 `sequence_gap` 时投影为 `stale`（仍有任务时为 `refreshing`），prompt 不得把旧观察断言成当前事实。
- renderer 把它标注为被动外部观察数据，不作为工具调用策略；何时调用 `video_understanding` 只由本轮动态 ToolSpec 描述和模型工具选择表达。
- `ContextBudgetReport`、token estimate、`ContextReport.sections.realtime_video_context` 和 `context.build.finished.realtime_video` 独立记账，不并入 conversation、memory、realtime task state 或普通 tool observation。

### Context Budget And Observability

- `ContextBudgetReport` 统计 request、conversation、memory、realtime video、plan、observations、tool specs、`owner_persona_chars` 和 total chars，并报告 `context_usage_ratio`、`compaction_triggered`；启用本地 token 估算时，最终实际注入的 persona 同时计入 `owner_persona_tokens` 和 `total_tokens`。
- `ContextBudgetReport` also tracks `tool_capability_chars` so the skill-style capability catalog is visible in budget/debug output. `AssistantContextPack` and `ContextReport` carry prompt-safe `skill_report_v1` fields for loaded, explicit, auto-candidate, selected, skipped, fallback, override, governed-tool, auto-recall reason, and permission issue visibility.
- 默认动态 context 字符预算是 12000 chars；identity recall 的固定 qualified tool/capability schema 在未显式配置硬预算时使用独立 headroom，不挤占 conversation/memory/observation 的默认预算。测试或特定调用通过 request metadata `context_budget_max_chars` 设置的值仍是包含工具 schema 在内的硬上限。
- 可选 token budget 字段包括 section token estimates、`total_tokens`、`max_tokens`、`token_usage_ratio`、`token_budget_source` 和 provider usage counters；它们只用于报告，不替代 char budget control path。
- 本地 token 估算通过 `context_budget_estimate_tokens=True` 或 `context_budget_max_tokens` 启用；provider usage metadata 如 `context_token_usage` / `provider_token_usage` / `last_chat_usage` 优先于估算。
- assistant loop 会把 `ChatResult.usage` 归一为安全 token counters 写入 request metadata，供下一轮 context budget report 使用；raw provider payload 字段不会写入 metadata/trace。
- 超过预算时会在 prompt 副本中裁剪 memory、conversation 和 observations，并记录 `over_budget`、`trimmed_chars`、`trimmed_sections`。
- `compression_stage` 记录 `none`、`compacted` 或 `budget_trimmed`；`compression_reasons` 记录 `conversation_context_compacted`、`observation_context_compacted`、`context_usage_high`、`tool_observation_too_large`、`provider_context_overflow`、`explicit_compact`、`context_over_budget`、`context_budget_trimmed`。
- 预算裁剪优先保留工具 observation，因为它通常是下一步工具调用和最终回答的证据来源。
- assistant decision trace 写入 `context_schema_version="context_observability_v1"`、budget、source counts、compaction summary、tool catalog summary、`compactor_type`、`context_summary_present` 和 memory promotion 计数；compaction summary 只暴露 pruning/truncation 计数，不暴露 raw payload；run/trace 查询会合并最终 save-memory 阶段的 redacted promotion counts。
- `react.decision` trace 或 native runtime 的 `context.report` 事件写入 `context_report_v1`，用于检查真实发送给 provider 的 system prompt 大小、selected native tool schema、memory 注入 ID、realtime task-state 大小和压缩/裁剪状态。
- Context pack construction emits standalone `context.build.started` /
  `context.build.finished` canonical trace events. The finished event carries the
  same redacted context summary shape used by trace/API context debugging.
- Trace sanitization 会过滤 `raw_provider_payload`、`raw_provider_response`、base64/media/file payload key 和 secret key，作为 public API 前的额外防线。
- `/runs/{run_id}` 与 `/traces/{trace_id}` 可查询 context 相关摘要。

### LLM Compactor And Provider Overflow

- `SummaryValidator` 要求 LLM compactor 输出完整旧 schema，并拒绝 secret/API key/base64/raw provider payload 等不应持久或注入的内容；可选 `handoff_v2` 同样经过递归安全校验，unsafe 或 invalid 输出回退 deterministic。
- LLM compactor prompt 会先移除 `raw_provider_payload`、`raw_payload`、`raw_html` 等高风险字段，再交给 provider。
- Summary 中如引用 `tool_call:` / `tool_call_id:`，必须保留对应 `tool_result:` / `tool_result_id:`，反向亦然，避免压缩时切断工具调用和结果证据链。
- Provider HTTP 413、`context_length_exceeded`、`context_overflow`、`input_too_large` 和 `request_too_large` 会归一为 `provider_context_overflow`。
- Context overflow retry 只允许一次；retry 计数和 provider error metadata 只记录安全摘要，不记录原始 provider response。

## Current Limitations

- 默认自动压缩仍是 deterministic formatting/summary。LLM semantic compaction 已有受控入口，但默认离线 profile 不启用。
- 当前全局压缩控制仍是 approximate character budget；recent transcript 和 memory context 已有独立 token-aware 边界，但这不替代 AssistantContextPack 的全局字符预算。
- Context Compiler v1 是调试/审计摘要，不是 prompt replay。它刻意不返回 raw prompt、raw provider payload、完整 memory 文本或完整 tool observation；token 字段仍依赖现有估算或 provider usage metadata。
- Editable owner context 当前只实现本机 owner-bound `SOUL.md`；没有实现 `USER.md` / `MEMORY.md` projection、skill L1/L2 view、Provider cache hint 或跨进程 last-known-good。
- 当前 memory retrieval 主要是本地关键词/片段匹配，不包含 embedding/vector retrieval。
- 会话历史压缩只增量合并滑出 token-aware recent window 的较早轮次，不做跨轮语义重写、事实抽取、冲突消解或质量反馈调参。
- assistant loop 的真实 LLM 路径中，长期记忆写入应由 assistant 通过 `memory_save` 工具显式选择；图尾不会自动写长期 task summary。

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
- `src/assistant_agent/memory/context_builder.py`
- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/memory/retrieval.py`
- `src/assistant_agent/agent/assistant_loop_nodes.py`

## Relevant Tests

- `tests/scopes/context/test_conversation_context_compaction.py`
- `tests/scopes/context/test_assistant_context_renderer.py`
- `tests/scopes/gateway/test_realtime_task_state.py`
- `tests/scopes/gateway/test_realtime_video_memory.py`
- `tests/scopes/gateway/test_realtime_video_observer.py`
- `tests/scopes/gateway/test_video_context.py`
- `tests/scopes/context/test_durable_task_context.py`
- `tests/scopes/context/test_shared_assistant_run_service.py`
- `tests/scopes/memory/test_memory_manager.py`
- `tests/scopes/memory/test_memory_context_builder.py`
- `tests/scopes/runtime/test_agent_communication_routing.py`
- `tests/scopes/runtime/test_react_action_quality.py`
- `tests/scopes/api/test_trace_query_api.py`
- `tests/scopes/context/test_context_sources.py`
- `tests/scopes/context/test_soul_context_source.py`
- `tests/scopes/runtime/test_improvement_evidence.py`
- `tests/scopes/runtime/test_improvement_detector.py`
- `tests/scopes/runtime/test_improvement_proposer.py`
- `tests/scopes/runtime/test_improvement_evaluator.py`

Current small regression coverage includes budget trimming order, product observation field preservation, prompt data-boundary labels, empty-query memory browsing, conversation compaction, trace context summaries, and run-summary context reporting.

## Next Steps

- Keep adding small regression tests when a concrete context failure appears.
- Consider broader token-aware control decisions only if recent transcript and reporting token fields show real provider failures that character budgeting cannot prevent.
- Consider semantic summary or embedding retrieval only after local relevance tests show keyword retrieval is insufficient.
