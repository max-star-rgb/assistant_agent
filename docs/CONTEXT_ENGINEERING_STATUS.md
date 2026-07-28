# Context Engineering Status

Last updated: 2026-07-28

本文件记录上下文工程的当前进展、已实现能力、限制和下一步方向。涉及 assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 的任务，应先读本文件顶部快速交接，再读对应小节、源码和测试。

## 新对话快速交接

如果新对话涉及上下文工程，先读本节即可快速接上当前状态。

- 当前结论：上下文工程已接入基于目标模型 tokenizer 的 rolling LLM summary；默认 mock/offline
  仍不调用压缩模型，real 运行需显式配置 LLM compactor 和本地 tokenizer 资产。
- 当前权威入口：本文件。
- 说明：已移除完成态阶段计划，当前以本文件作为上下文工程状态与交接入口。
- 已实现核心闭环：`AssistantContextPack`、`ContextSection v1`、默认关闭的 local owner `SOUL.md`
  source、Context Compiler v1、完整 Provider-native transcript、rolling natural-language session summary、
  model-tokenizer preflight、独立 `realtime_video_context`、durable task state、tool observation、tool schema、
  结构化 RunToolCatalog 和 trace/API 上下文摘要。
- `PromptCompiler` 先生成完整 `ChatRequest`，`ContextTokenCounter` 再对 messages、tools、tool choice 和
  response format 的稳定 payload projection 分词。达到有效输入窗口 70% 时，`LLMCompactor` 合并旧摘要
  与全部已完成原始轮次；成功后被覆盖轮次从 `ConversationStore` 删除，不保留 recent raw turn。
- 默认阈值为 target=40%、trigger=70%、hard=85%。70%-85% 的压缩失败保留原文并继续；hard 区间重试
  一次后仍失败则停止主模型调用并返回稳定错误。Provider context overflow 也强制进入同一 hard 流程。
- ContextBuilder 不执行字符容量裁剪。当前请求、本 run 未闭合的 native tool call/result、system policy、
  tool schema、memory 和其他 section 不进入 conversation summary。
- memory 边界：长期记忆只来自 Mem0。session 创建时按可信身份调用一次 Mem0 `get_all` 并冻结
  snapshot；包括第一轮在内的所有 turn 只复用 snapshot，不再召回。成功 turn 在回复提交后把
  user/assistant messages 异步交给 Mem0 原生 `add`，由 Mem0 负责提取、合并、向量化和持久化。
- session memory snapshot 只保存从 Mem0 原始记录提取的结构化 `LongTermMemory`。不存在 memory read/write
  policy、ranking、profile、promotion 或 memory tool。ContextBuilder 在每轮把同一份冻结 items
  写入 context pack；Context renderer 再将原始文本按顺序组装为 XML 转义的
  `<long_term_memory trust="untrusted_history">` 历史证据，与 `<current_request>` 形成显式边界后
  进入当前 `user` message，不进入 `system` message。
- realtime video 交接：Agent-Service 后台 Qwen observer 对每个 `video_id` 复用一个 persistent WebSocket 并预热 rolling 语义；VLM 使用独立视觉角色模板 prompt，只产出结构化视觉事实，不复用主 LLM 系统提示。普通图片/显式视频只暴露 `media_inspect`，可信实时媒体会话只暴露 `live_view_inspect`；后台 observer 使用不进入模型目录的 `realtime_video_observe`。主 LLM 不包含 VLM 观察流程、OCR/品牌/序列图等视觉分析提示词，也不看到帧、JPEG 路径、base64、VLM prompt 或 provider raw response。
- 当前不建议继续做：场景分类器、质量反馈自动调参、组件注册器、裁剪 undo 日志或把长期记忆并入 session summary。
- 如果用户问“继续上下文工程”：优先做验收案例、调试说明、具体失败复现和小回归测试；不要默认新增复杂架构。
- 按需补读：解释机制时读本文件对应小节；涉及长期记忆写入/检索时读 `docs/memory-service-architecture.md`。

## Current Stage

上下文工程已进入可用实现和硬化阶段，不是单纯规划。

- 多阶段 Context Engine + Memory Policy 计划已经完成；后续应把 `docs/CONTEXT_ENGINEERING_STATUS.md` 作为当前入口。
- 主运行时是 LangGraph/ReAct assistant loop，默认 mock/local/offline。
- LangGraph 拓扑保持 `assistant -> execute_tool -> assistant`；`ContextService` 作为非 checkpoint
  runtime dependency 注入 assistant node，统一负责 context pack 构建、Provider-native request
  编译、token preflight、rolling compaction 和压缩后重建。assistant node 只保留工具目录选择、
  模型 turn 调度、决策 guard 与状态归并，不为纯上下文计算增加 graph node。
- `AssistantContextPack` 已接入 assistant 每轮决策，统一收集 request、conversation、memory、realtime video、plan state、tool observations、tool specs、source counts 和 budget。
- `AgentGraphRuntime` 可在 run 入口通过 `ContextSourceCoordinator` 加载一次显式 owner-bound 的 `SOUL.md`，把验证后的 `ContextSourceResult` 冻结到 `AgentState`；同一 run 的多次 assistant iteration 不重复读文件，下一 run 才观察合法更新。
- 生产 provider-native `ChatRequest` 统一通过无副作用 `PromptCompiler` 编译；真实与 mock provider 共用
  LangGraph assistant loop。Runtime 显式区分 `ACT` 与 `FINALIZE`：工具预算耗尽或 guard 要求停止行动
  后进入 `FINALIZE`，由 `PromptCompiler` 重建只含用户上下文和结构化 evidence 的独立请求，不沿用
  当前 run 的 native tool-call 轨迹。legacy prompt-json renderer 仍只用于离线兼容与测试。
- 通用 system prompt 在每次编译时把带时区的可信本地运行时间和部署侧配置的当前位置放入独立的
  `当前环境` 段，确保 Provider 原始 input 和受长度限制的开发预览都优先显示。当前日期、星期、
  时间和相对日期解析以本地时间为准；用户未指定目标地点时可把已配置的当前位置作为默认值，
  用户明确指定地点时则始终以用户输入为准。当前位置通过
  `MULTIMODAL_AGENT_CURRENT_LOCATION` 配置，未配置时使用上海并明确标记为默认地点，不伪装成动态
  定位；用户指定地点仍始终优先。这些事实不承担
  天气、新闻等外部动态信息查询，外部事实仍必须使用已暴露工具。
- Provider-native Prompt 不注入 capability catalog 或 Skill descriptor，也不根据 `request.text` 做关键词、
  正则或确定性意图召回。模型只通过本轮结构化资格化后的原生 `ToolSpec` schema 了解候选工具。
- Context Compiler v1 以 `ContextReport` 暴露每次 LLM call 的 redacted section accounting：`system_prompt`、`request`、`session_summary`、`recent_transcript`、`memory`、`realtime_video_context`、`durable_task_state`、`plan_state`、`tool_observations` 和 `tool_schema`，并以非累加的 `context_source_report_v1` 报告 section kind/authority/stability 字符数、稳定 issue code、last-known-good 和版本变化计数；不暴露 SOUL 原文、source version、绝对路径、完整 prompt、memory 文本、视频摘要、tool observation 或 provider payload。兼容 schema 仍保留 `realtime_task_state` section，但编译时始终为未包含。
- `ContextBudgetReport` 明确是压缩前后的 `precompile_estimate`；`ContextReport.accounting_basis=compiled_chat_request` 则直接核算同一 `PromptCompiler` 产出的 messages、tools 和 response_format。二者不再冒充同一口径，report 同时保留 `budget_estimated_chars` 便于解释差值。
- 上一轮 Provider usage 只保留在 `provider_*_tokens` 诊断字段，标记为 `previous_provider_usage`，不再写入当前待发送 context 的 `total_tokens`。

### Durable Task Context

- worker resume 只把 Pydantic 校验通过的 `request.metadata.durable_task_snapshot` 转成 `AssistantContextPack.durable_task_state`。普通入口会移除外部传入的 snapshot/binding/lease 等保留键，不能用请求 metadata 伪造 worker 状态。
- trusted resume 读取 worker 注入的 `ready_tool_names`，只向模型展示当前 ready tools 与 `task_plan_submit`；普通 foreground 直接展示全部通过本轮结构化治理的 ToolSpec。
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
- `MULTIMODAL_AGENT_CONTEXT_COMPACTOR=llm` 只在 real Provider 模式生效；还必须提供
  `MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH=<local tokenizer.json>`。tokenizer loader 禁止联网下载。
- `tokenizers>=0.20,<1` 是项目运行时依赖；服务只从上述显式本地路径加载资产，不通过
  `huggingface_hub` 自动解析模型或联网下载。
- `MULTIMODAL_AGENT_CONTEXT_INPUT_TOKEN_LIMIT` 必须匹配实际 endpoint/deployment；`qwen3.6-flash`
  [公共模型](https://help.aliyun.com/zh/model-studio/vision-model)默认 1,000,000、最大输出
  64,000；专属部署或其他路由应由 operator 覆盖。
- `ContextWindowPolicy` 使用 40%/70%/85% 和 safety margin 形成回滞；阈值、输入上限、安全余量及
  summary 最大输出均可通过进程配置覆盖。
- `LLMCompactor` 输出带七个固定中文标题的自然语言正文；`ContextSummary` 只以结构化 envelope 保存
  revision、覆盖轮数、source/summary token、模型和最后覆盖的 run/trace 边界。Runtime 不使用
  deterministic fallback。
- `TokenBudgetReporter` 的旧字符估算仅保留兼容报告；压缩控制面读取最终 compiled request 的 tokenizer
  preflight。Provider `usage` 在调用后写回预检记录，用于观测本地计数与实际 input token 的误差。
- AgentRuntime 保留完整内部 tool observation 供 trace/runtime 使用；进入 Provider prompt 的副本会先移除
  secret、raw provider/file/media payload 和 inline media data URI，再执行确定性的字符串、列表、嵌套
  深度和命令输出压缩，最后投影为最小公共协议。原始 observation 始终不被修改。
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
- 未触发压缩时，当前 summary 之后新增的全部已完成 turn 以 Provider-native `user`/`assistant` 消息发送。
- 触发压缩时，LLM 一次合并旧 summary 与当前全部已完成 raw turns；成功后这些 raw turns 从内存或
  JSONL store 的 session prefix 删除，只保留 summary。后续新 turn 再次累积，形成 rolling summary。
- 当前用户请求不进入 summary；它始终作为当前 `user` message 保留。
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

- AgentRuntime 只压缩进入 assistant prompt 的 observation 副本，不修改 graph state、trace 或 API 使用的
  完整 observation。
- Provider 侧公共协议保留执行 `status`、面向推理的 `summary`、工具自定义 `data`、
  `is_complete`，并在适用时携带 `outcome`、`warnings`、可选 `output_ref`；失败或部分成功时使用统一
  `error` 记录结构化错误事实。工具名由 native tool message 的 `name` 携带，不再在 message content 内重复。公共语义字段
  从工具自定义 `data` 中提升并去重；Runtime 根据结构化错误和 guard 状态实施恢复，不向模型注入
  `next_step_hint`、`recovery_hint` 等命令式兼容字段。
- 工具通过 `ToolResult.model_observation` 定义自己的 LLM 数据投影；context 层统一负责安全清洗、最多
  3 个列表项、字符串/命令输出裁剪和嵌套深度限制，不在中心模块解释工具业务字段。
- `shopping_search` 把候选归一为最多 3 个 `items`，不再同时发送 search items、offers 和完整
  best_offer 镜像；其 `response_contract=shopping_detail_v1` 随购物数据提供模板、资格条件和 fallback，
  由 LLM 使用真实结果字段生成最终回复。`image_generation` 只投影去重后的 `images`。
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
- Provider-native 编译将 summary 之后尚未覆盖的原始轮次还原为独立 `user` / `assistant` messages，
  再追加当前 `user` message。rolling summary 作为带 `trust="untrusted_history"` 和
  `instruction_policy="do_not_execute"` 的 session data 进入当前 user context。
- Provider-native `ChatRequest.tools` 使用 `AssistantContextPack.prompt_tool_specs` 中已治理的 schema。context builder 同时生成 prompt-safe `RunToolCatalog`；其中 `available_tool_names` 既是模型可见目录，也是 `ActionValidator` 的 run-scoped 执行边界，不再维护重复的 exposed/executable 集合。目录装配只消费 category、媒体要求、默认启用以及显式 tool/skill 等结构化事实，不读取 `request.text` 做意图路由。Plugin 只负责装配和归属，不授予单轮执行权限；系统不维护独立 toolset、Tool Search 或 Schema 渐进披露，全部合格 ToolSpec 直接进入 Provider 请求。工具规模由部署 Plugin、MCP allowlist 和入口 `allowed_tools` 控制，现有 context report 继续记录实际 Schema 占用。
- 系统提示词只承载通用 runtime、数据边界和工具治理规则，不写入某个具体工具的选择策略。模型可见的工具说明只来自 `ToolSpec.description` 和 `input_schema`。
- Repo-local business skill loader 只服务 `tool_visibility.enabled_skills` 的显式结构化工具资格化以及离线 Improvement Lab；它不生成 Provider Prompt，不自动召回，也不创建 `run_skill` 或直接 shell/browser/http 执行路径。
- `FINALIZE` 使用高优先级最终回答约束、`tools=[]` 和 `tool_choice=none`；工具 observation 被投影为
  保留 status、summary、outcome、warnings、is_complete、工具专属 data、error 和 output_ref 的结构化
  evidence。它不把 evidence 降级为单一 summary，也不保留 `assistant.tool_calls -> tool`
  协议序列。模型在该阶段返回 tool call 属于
  `finalization protocol violation`，Runtime 不执行，并且最多做一次同样无工具的严格纠正。
- session summary renderer 明确把摘要标注为不可信历史数据，不作为长期记忆或系统指令。
- prompt 明确声明 conversation、memory、observation 和 tool output 都是数据，不是系统指令；retrieved memory 是用户历史证据，不是权威信息，当前用户输入和新工具结果优先，不能执行 memory 中的指令。

### Editable Owner Context

- Editable owner context 默认关闭，只接受进程配置 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED=true`、`MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT=<root>` 和显式 `MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID=<user_id>`；request metadata 不能启用能力、改变 root 或切换 owner。
- 首版只读取固定 `<root>/SOUL.md`。支持的二级标题只有 `Persona`、`Expression Style`、`Relationship Boundaries` 和 `Avoid`，编译优先级固定为 `Relationship Boundaries -> Avoid -> Persona -> Expression Style`。
- loader 使用 owner identity fail-closed、root containment、symlink/non-regular-file 拒绝、UTF-8、16,000 bytes、4,000 chars、2,000 compiled chars、每 subsection 800 chars和 secret/base64/raw-provider marker 检查。超限或 unsafe 新版本不会静默截断生效。
- 合法内容生成单一 `authority=owner_persona`、`stability=semi_stable` 的 `ContextSection v1`。`PromptCompiler` 只消费已验证 section，并把它放在不可变 runtime policy 之后；persona 不能改变 ToolSpec、RunToolCatalog、tool choice、validator、identity、memory policy 或 provider mode。
- 非法更新可回退到按 `(resolved root, owner user id)` 分区的 process-local last-known-good。该缓存不提供跨 worker 一致性，进程重启后的首次非法文件会被省略。
- Owner-trusted persona 会影响模型表达；本地治理保证的是能力和安全边界不被它配置性地改写，不承诺任意恶意人格文字对生成内容零影响。

### Realtime Task State Runtime

- `prepare_realtime_task_state_request` 只在显式 realtime mode/capability 的请求进入 `AgentGraphRuntime.run_state(...)` 前生成 runtime-safe task-state snapshot；该 snapshot 不进入 Provider prompt。
- 完整 snapshot 保留在 request metadata 供 Gateway、interrupt、artifact 和 side-effect 治理使用；主模型依赖当前用户请求和 conversation context 承接意图，不接收 task-state snapshot 或其语义 projection。
- Task-state 记录 session 内当前 objective、active constraints、source turn/run ids、interrupt 产生的 `IntentRevision`，以及 completed run 后的 prompt-safe `TaskArtifact`、lightweight checkpoint artifact 和 `SideEffectRecord`。
- Task-state 现在也记录 prompt-safe realtime call state：`pending_tool`、`tts_state`、`last_spoken_progress`、`speech_turn_id`、`barge_in_source` 和 bounded `last_realtime_event_ids`，用于表达工具等待、展示/TTS 状态和打断来源；工具完成/失败、取消和挂断会清理 pending tool，TTS/display started/finished/superseded 会更新展示状态；不保存 raw audio、raw transcript stream 或 provider payload。
- `pending_tool` 会消费 `tool_started` 事件中的 prompt-safe `pre_tool_call` 摘要，保留工具副作用等级和 idempotency key 摘要，便于 interrupt 后选择重规划、去重或补偿路径。
- Interrupt run 的 snapshot 会保留原始 objective，并把最新 interrupt 文本写入 `latest_revision`；普通 queued follow-up 只更新 current user text 和 provenance，不创建 revision。
- Completed realtime run 会按 `ToolSpec.execution.artifact_reuse` 把 selected tool observations 和 media refs 记录为 task artifacts；tool observation artifact 复用现有 prompt compaction 逻辑，不保存 raw provider/file/media payload。
- 多步 realtime run 在同一轮完成至少两个 reusable tool observations 时，会记录 bounded `checkpoint` artifact；interrupt 只有在 checkpoint 仍可复用时才选择 `resume_from_checkpoint`，用户明确重来/换一批会把 checkpoint 标为 stale。
- Interrupt 会用简单策略选择 `restart`、`reuse_and_replan`、`report_committed` 或 `compensate`；如果用户明确要求重新搜索/换一批/不要之前结果，已有 reusable artifacts 会标记为 `stale`，不会重新注入 prompt snapshot。
- Side-effect records 来自 Tool category 和工具结果中的 prompt-safe override（例如 `side_effect_level`）；read-only 工具不阻塞重规划，committed action 不会被描述成已取消，compensatable artifact 会倾向修正版/补偿路径。
- `GatewayRuntimeAdapter` 和 shared run service 会发 display-only `run.progress`，用于 App + Media 展示 `task_state/revising`、strategy、reusable artifact count 和 side-effect count。
- Realtime delivery policy 将 `run.progress` 和 tool lifecycle 标记为 `persistence=ephemeral`，只有 `response.chunk` 属于 `persistence=final`；progress 使用 run-scoped replacement key，并由 final chunk 或 `run.end` supersede，因此不会作为 assistant final text 进入 conversation history 或长期 memory。
- 当前写工具在被 Tool Catalog 暴露并通过 Validator 后直接执行；read-only 工具无额外开销，compensatable 工具可去重。durable task 有独立 SQLite task/lease 恢复与身份隔离 API；跨进程幂等 ledger 仍未接入。

### Realtime Video Context

- Agent-Service 的后台 observer 继续通过内部 `realtime_video_observe` 和工具治理链执行 Qwen；“后台受治理工具执行”和“AgentRuntime 可见工具目录”是两个边界。有 active video 的可信 AgentRuntime turn 只动态暴露 `live_view_inspect`，普通附件工具 `media_inspect` 不进入该目录；主 LLM 只看到 prompt-safe 文本上下文，不见帧、内部媒体路径或 VLM 角色模板。
- `AgentGraphRuntime` 在每次模型 context build 前按请求中最后一个 `video_id` 重新投影共享 `RealtimeVideoMemoryStore`，生成 `ready`、`refreshing`、`pending`、`stale`、`failed` 或 `unavailable` 状态。
- 可信 Agent-Service 入口把 `video_ids` 渲染为“当前共享的实时画面”而不是上传式“附带视频 ID”；AgentRuntime system prompt 保持入口和通道无关，不注入电话、口播、挂断或传输层规则。普通上传/API 仍保留上传语义。
- observer 首帧必选、明显变化立即候选、静态画面最长 2 秒产生一次候选；队列保持一个 Qwen in-flight 和一个 latest-wins pending。每轮 Provider 请求只含当前单帧和最多 2,000 字符的上一成功语义摘要，不重发多帧历史。
- 明确指代当前画面的问题由 AgentRuntime 主 LLM 通过动态暴露的 `live_view_inspect` 表达；该工具只读取后台滚动语义快照，入口不放视觉分析提示词，也不基于文本自行完成 VLM 判断。问候/闲聊不应主动提及视觉。
- 每个 `video_id` 复用一个 persistent Qwen WebSocket；20 次成功观察或 60 秒后轮换，断线按 0.25/0.5/1/2/5 秒封顶退避重连。失败保留最后成功快照并投影 `refreshing`/`stale`；关闭或切换 video id 会关闭 Provider session 并清理 pending、语义状态和帧文件。
- 投影只包含裁剪后的 summary、objects、people、actions、events、scene、`snapshot_sequence`、`target_sequence`、`completed_sequence`、`sequence_gap`、观察耗时、provider/model、`transport`、`session_generation`、`connection_reused`、`reconnect_count` 和 pending/in-flight 状态，序列化上限约 2,000 字符；不含帧路径、媒体数据、Qwen 原文、raw event 或 provider 原始错误。
- `snapshot_age_ms` 与 `frame_capture_age_ms` 以成功语义对应帧的采集时间为主，`snapshot_publish_age_ms` 独立表示结果发布时间年龄；缺失或未来采集时间返回空值，不伪造年龄。存在正 `sequence_gap` 时投影为 `stale`（仍有任务时为 `refreshing`），prompt 不得把旧观察断言成当前事实。
- renderer 把它标注为被动外部观察数据，不作为工具调用策略；何时调用 `live_view_inspect` 只由本轮动态 ToolSpec 描述和模型工具选择表达。
- `ContextBudgetReport`、token estimate、`ContextReport.sections.realtime_video_context` 和 `context.build.finished.realtime_video` 独立记账，不并入 conversation、memory、realtime task state 或普通 tool observation。

### Context Budget And Observability

- `ContextBudgetReport` 统计 request、conversation、memory、realtime video、plan、observations、tool specs、`owner_persona_chars` 和 total chars，并报告 `context_usage_ratio`、`compaction_triggered`；启用本地 token 估算时，最终实际注入的 persona 同时计入 `owner_persona_tokens` 和 `total_tokens`。
- Context budget 仍以默认 12000 chars 生成观测报告；即使超过该值也不裁剪。
- `context_token_preflight_v1` 记录 tokenizer id、compiled input tokens、effective input limit、usage ratio、
  target、triggered 和 hard；ContextReport 使用该结果作为 compiled request token 口径。
- 压缩成功时，`context_token_preflight_before_compaction` 保存压缩前快照，主
  `context_token_preflight` 更新为最终发送请求的计数，避免 Provider usage 与错误基线比较。
- assistant loop 会把 `ChatResult.usage` 归一为安全 token counters，并在同一 preflight 记录中追加
  Provider prompt tokens、误差 token 和误差比例；raw Provider payload 不进入 metadata/trace。
- 摘要模型每次成功或输出校验失败尝试的 token 消耗单独追加到
  `context_compaction_provider_usage_history`，不与主回答调用的 usage 或 preflight 误差混算。
- 超过预算只记录 `over_budget`；AgentRuntime 不在 prompt 副本中裁剪 memory、conversation 或 observations。
- `compression_stage` 记录 `none`、`compacted` 或 `budget_trimmed`；`compression_reasons` 记录 `conversation_context_compacted`、`observation_context_compacted`、`context_usage_high`、`tool_observation_too_large`、`provider_context_overflow`、`explicit_compact`、`context_over_budget`、`context_budget_trimmed`。
- 预算裁剪优先保留工具 observation，因为它通常是下一步工具调用和最终回答的证据来源。
- assistant decision trace 只记录归一化决策、工具名、reason 和 plan 状态，不重复携带 context
  summary 或 report。
- canonical `context.build.finished` 独占 `context_report_v1`，用于检查真实发送给 Provider 的 system prompt
  大小、selected native tool schema、memory 注入 ID、realtime task-state 大小和压缩/裁剪状态。
- Context pack construction emits standalone `context.build.started` /
  `context.build.finished` canonical trace events. The finished event carries the
  same redacted context summary shape used by trace/API context debugging，并额外携带
  prompt-safe `context_report_v1` section accounting。Langfuse 将该 observation 展示为
  `context.compile`，output 明确标记为编译报告，并只附带 message roles/count、tool count 和
  response-format presence；`build_reason` 区分 `iteration_initial`、`post_compaction` 和
  `provider_overflow_retry`。最终 compiled `ChatRequest` 正文仍只归属同 iteration 的
  `llm.chat` generation input；同 iteration 后续出现的 `context.compile` 会取代较早报告，
  避免把初始候选报告误读为最终上下文本身。
- Trace sanitization 会过滤 `raw_provider_payload`、`raw_provider_response`、base64/media/file payload key 和 secret key，作为 public API 前的额外防线。
- `/runs/{run_id}` 与 `/traces/{trace_id}` 可查询 context 相关摘要。

### LLM Compactor And Provider Overflow

- `SummaryValidator` 要求自然语言摘要包含当前目标、用户约束与偏好、已确认事实、已执行操作与结果、
  已作出的决定、未解决事项和最近交互状态七节，并拒绝 secret、data URI 等不安全输出。
- compactor 输入只包含旧 summary 和已完成 user/assistant turn；不包含当前请求、当前 run tool
  observation、Provider raw response、base64 或 secret 字段。
- 当前 run 的 `assistant.tool_calls` 与 `role=tool` 消息始终留在原生消息序列，压缩不得拆散调用结果对。
- Provider HTTP 413、`context_length_exceeded`、`context_overflow`、`input_too_large` 和 `request_too_large` 会归一为 `provider_context_overflow`。
- Provider context overflow 归一为稳定错误并强制进行一次 hard compaction；无可压缩历史或重试失败时停止。

## Current Limitations

- tokenizer preflight 是 Provider payload projection，不包含 Provider 私有 chat template 的精确内部开销；
  safety margin 和调用后的 Provider usage 误差用于控制该差异。
- rolling LLM compaction 仍只压缩 conversation history；当前 run tool observation 在进入 prompt 前会做
  确定性投影和局部容量限制，但如果 system、memory、tool schema、durable state 或投影后的当前 run
  observation 总体仍超过 hard limit，Runtime 会返回稳定错误。
- Context Compiler v1 是调试/审计摘要，不是 prompt replay。它刻意不返回 raw prompt、raw provider payload、完整 memory 文本或完整 tool observation；token 字段仍依赖现有估算或 provider usage metadata。
- 显式本地 trace-content + loopback OTLP 模式是独立的 prompt 调试例外：assistant loop
  会在 Provider 调用前把最终 compiled `ChatRequest` 暂存到进程内 store，并作为对应
  Langfuse `llm.chat` generation input 导出。该能力不改变 Context Compiler/API 的摘要契约，
  不写 JSONL，不保存 Provider 原始响应或 hidden reasoning。
- Editable owner context 当前只实现本机 owner-bound `SOUL.md`；没有实现 `USER.md` / `MEMORY.md` projection、skill L1/L2 view、Provider cache hint 或跨进程 last-known-good。
- memory extraction、向量化和持久化全部由 Mem0 实现，项目不维护第二套检索算法。
- LLM summary 的语义保真属于 eval 边界；pytest 只验证触发、替换、失败分级、持久化和 native tool 配对。
- assistant loop 不向主 LLM 暴露任何 memory tool；主 LLM 只消费 session 启动时冻结的 Mem0 snapshot。

## Key Files

- `src/assistant_agent/context/builder.py`
- `src/assistant_agent/context/conversation.py`
- `src/assistant_agent/context/compaction.py`
- `src/assistant_agent/context/policy.py`
- `src/assistant_agent/context/token_budget.py`
- `src/assistant_agent/context/token_counter.py`
- `src/assistant_agent/context/compactor.py`
- `src/assistant_agent/context/renderer.py`
- `src/assistant_agent/context/report.py`
- `src/assistant_agent/skills/loading.py`
- `src/assistant_agent/context/sources.py`
- `src/assistant_agent/context/soul_source.py`
- `src/assistant_agent/runtime/realtime_task_state.py`
- `src/assistant_agent/memory/service.py`
- `src/assistant_agent/memory/models.py`
- `src/assistant_agent/memory/session_snapshot.py`
- `src/assistant_agent/media/video/realtime_video_memory.py`
- `src/assistant_agent/media/video/realtime_video_observer.py`
- `src/assistant_agent/media/video/video_context.py`
- `src/assistant_agent/automation/durable_tasks/`
- `src/assistant_agent/multi_agent/agent_delegation_context.py`
- `src/assistant_agent/context/models.py`
- `src/assistant_agent/context/service.py`
- `src/assistant_agent/runtime/assistant_run_service.py`
- `src/assistant_agent/runtime/chat_adapter.py`
- `src/assistant_agent/providers/provider_errors.py`
- `src/assistant_agent/improvement/`
- `src/assistant_agent/improvement/models.py`
- `scripts/run_improvement_lab.py`
- `src/assistant_agent/memory/mem0/client.py`
- `src/assistant_agent/memory/ingestion_queue.py`
- `src/assistant_agent/runtime/assistant_loop_nodes.py`

## Validation Boundary

pytest 通过 scripted adapter 和 fake tokenizer 保护 rolling summary 的稳定代码契约：70% 触发、40% target、
85% hard failure、连续滚动、内存/JSONL 历史前缀替换、Provider usage 误差记录以及当前 native tool
call/result 配对。真实 Qwen tokenizer 误差和摘要语义质量属于 system/case eval，不得在 pytest 调用真实
Provider。

## Next Steps

- 使用真实 `qwen3.6-flash` system eval 校准 tokenizer projection 与 Provider `input_tokens` 的误差，再决定
  是否调整 safety margin。
- 使用多轮 case eval 评估七节自然语言摘要对约束、数字、否定、未完成事项和工具结论的保真度。
- 只有固定 context section 经常单独逼近 hard limit 时，才设计 conversation 之外的容量治理。
