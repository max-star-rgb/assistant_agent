# LangChain-native Context Engineering

最后更新：2026-09-04

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 统一生产 Agent 的标准 messages、dynamic prompt、state 投影、预算与 summarization 权威 |
| Owns | dynamic system prompt、Memory 数据边界、标准 message history、task state allowlist 与官方 summarization middleware |
| Does not own | Tool schema、Memory backend、Provider wire、媒体 frame、旧 ContextService |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_prompt.py`、`native_agent/assistant_agent.py`、`native_agent/user_context.py`、`native_agent/context.py`、`native_agent/state.py`、`native_agent/token_counter.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 当前主链

只读工具自动重试耗尽后，`ReadToolRetryMiddleware` 仅在下一轮模型请求的消息投影中追加
`HumanMessage(name="tool_recovery")`，说明失败并建议结合联网搜索直接回答。该消息明确标注为运行反馈，
不代表用户新请求，不写入 checkpoint/history，不复制外部异常原文；重试策略由 Tool authority 所有。

Qwen Audio Gateway 前台通过原生目录配置加载项目 `integrations/qwen_audio_agent/prompts/PROMPT.md`，
以 qwen-audio-agent 2.0.1 官方正文为基础，仅直接修改记忆个性化规则；默认人设继续加载官方 `ASSISTANT.md`。
复用原生 `buildFrontendInstructions`、`resolveAssistantProfile`、`buildMemoryContext`。
项目另维护 `integrations/qwen_audio_agent/prompts/BACKEND_TASK.md`（工具描述）、`TIME_LOCATION.md`（时间地点）。
记忆使用说明直接加载上游 `server/src/memory/PROMPT.md` 原文，不保留项目副本。日期按 `Asia/Shanghai` 在构建时替换 `{{current_date}}`，地点固定为上海西岑地铁站。
时间地点以 `current_date`、`location` 字段合并进原生 `runtime_context`，不再生成独立的“时间与地点”段落。
记忆规则通过原生 memory feature 注入；文本数据由原生 Markdown provider 和订阅直接提供，不再注入外部 Mem0 快照。
`USER.md` 使用原生 `<user_preferences>`，`MEMORY.md` 使用 `<user_memory>`，保留版本与空内容省略机制。
远端视觉快照单独作为 `<visual_memory>` 注入前台提示词，区域内仅按原顺序输出文本，条目间空一行，不显示 JSON、字段名、状态或说明文案；保留标签边界转义，不改写、合并或去重文本。
会话初始化先读取快照，此后每处理完成 10 个视频段异步刷新；更新期间使用旧快照，成功空结果清空文本但保留该区域，状态仅留在内部快照，不进入 Markdown 或会话提取。生命周期见 Memory authority。
主 `PROMPT.md` 的个性化顺序为当前明确要求、用户明确表达的长期偏好、默认人设；系统推测不提升为用户要求。
历史事实与视觉观察只是可能过时的背景，不授予权限。文本外部快照补丁已撤销，视觉快照保持独立。
语音前台沿用官方交互和授权机制，明确请求直接执行，需要确认时在对话中澄清。
Gateway 业务委派关闭 LangGraph 原生 HITL，不提供自定义审批工具、同工具授权缓存或审批纠正响应。
该策略只适用于 Qwen Audio Gateway，不修改独立 `/media-lab` 的 Voice 或 Studio/SDK 的显式审批配置。

本机安装客户端新增可选公开字段 `device_id`。它只选择已由服务注册并绑定当前 identity/cwd 的执行连接，
不代表用户身份或授权凭据；省略时保持既有 Studio/媒体行为。安装客户端显式开启 `require_tool_approval`。
此阶段只开放同机注册，但项目指令和 cwd Skills 已由绑定客户端读取，服务端不再依赖同名路径。
源码 Skills 仍随服务发布。Deep Agents 生成的压缩历史保存在执行目录 `.agent/history`；原有 Home 历史不自动迁移。

浏览器持续语音 PCM 直接转发 Provider，原生 Realtime 会话不经过 Graph middleware 或占位消息。
模型返回的 ASR 与回复转写用于语音 trace，不作为业务输入历史。浏览器委派只有一条
`HumanMessage(name="voice_agent", content=request)`，request 最多 16,000 字符，必要背景由 Voice 自行写入。
同一会话复用独立业务 thread，新委派以 rollback 替换冲突的旧 run；已完成业务历史保留在业务 checkpoint。
业务任务关闭 Memory，显式开启工具审批。挂断或说话打断不回滚任务。
Graph 驱动的 Voice 与分段音频兼容路径已删除。

业务图上下文以 LangChain 标准 `messages` channel 为事实源，Studio/SDK 直接输入业务图。
原生 Voice 使用 `prompts/voice.py`，委派 request 是普通 HumanMessage，不提升为系统指令。
不再保留同步 Voice 子图委派、历史业务 artifact 回投或父 Voice state 投影。
以下分层 Prompt Builder、Skills 和执行上下文规则适用于内部 `AssistantAgent`：其 dynamic prompt 的 `general` 使用
`native_agent/prompts/studio.py`，`call_assistant` 使用 `native_agent/prompts/media.py`。Studio 默认 `general`，
媒体握手默认 `call_assistant`，均可显式覆盖；入口事实不再决定提示词。Deep Agents Skills L0 目录随后注入，dynamic prompt 最后追加当前工作目录、
项目指令、北京时间自然日、可信用户地区与本轮媒体事实。当前工作目录来自公开 `AssistantRunContext.cwd`；Studio
网页、媒体和 Studio/SDK 默认共用 `~/assistant_cqy/`；显式 cwd 或设备绑定保留。
工作目录按文件能力需要创建，Server 启动不预建目录或空 `ASSISTANT.md`；`.agent/browser`、`.agent/downloads` 与 `outputs` 由对应能力按需创建。
通用 backend 构造、目录读取和 Skills discovery 不创建目录；不存在的 Skills 目录返回空。只有实际写入按原生 filesystem backend 语义创建目标父目录。Shell 要求 cwd 已存在。异步 backend 准备与 Skills 目录检查在工作线程执行，不阻塞 Agent Server 事件循环。
工作目录 prompt 明确普通下载默认保存到 `<cwd>/.agent/downloads/`，正式交付文件保存到 `<cwd>/outputs/`，浏览器使用配置的 `<cwd>/.agent/browser/`。这属于默认执行约定，不是文件系统沙箱；显式用户路径仍可覆盖。
项目指令只沿 Home 到 cwd 的祖先链读取 `ASSISTANT.md`，单次最多注入 32 KiB，不扫描 Home 的其他目录。
预算不足时优先保留最接近 cwd 的指令，再以祖先到子目录的顺序呈现；自动加载拒绝解析到 Home 外的
指令文件 symlink，避免把 cwd 选择扩大为隐式文件读取授权。最终 runtime dynamic prompt 将上游 middleware
生成的普通纯文本 section 合并为单个 text content block；带额外元数据或非文本 block 保持原结构。

稳定策略要求直接推进结果，只在缺少阻塞信息或关键选择会改变结果时询问，并区分
已核验事实、失败和模型判断。Git 仓库识别与命令执行由 `git` Tool 的代码边界负责，不写入 system prompt。
两套 persona 的「搜索」段落要求优先直接调用已注册且默认暴露的
`mcp_bailian_bailian_web_search` 查资料和核实最新事实，不使用模型原生联网搜索。
browser 用于网页交互，也可在搜索工具不可用时搜索；curl 经可用的 execute 请求已知 URL/API、检查响应或下载文件。
这些文案只指导模型选择已有能力，不改变 Tool exposure、Provider 配置或审批边界。

公开 `AssistantRunContext` 包含 `persona`、`cwd`、`enable_memory`、`require_tool_approval`，以及 Studio 可配置的成对绝对值
`context_compaction_trigger_tokens` / `context_compaction_keep_tokens`。压缩值都不设置时使用 Provider context window 的
75%/15%，设置时必须满足 `0 < keep < trigger`，并由 业务/worker 的 Deep Agents
`SummarizationMiddleware.awrap_model_call` 在当前 run 中读取而不修改进程共享 middleware。
业务 factory 保留 `call_assistant` 的 128-token 策略供独立调用，但生产委派固定 `general`，不限制业务输出为口语长度。
Voice 使用独立 Realtime 会话，worker 固定使用通用策略与默认上限。`enable_memory` 默认 true，只控制业务图本轮 recall；语音委派显式关闭。记忆提取调度仅由前台 Realtime 会话关闭触发。
`require_tool_approval` 在本机开发默认 false，可由单次 run 显式开启已配置 Tool 的 HITL interrupt；两者都不进入 prompt。`entry_profile` 与视觉 capability token 属于服务端签发的
`AssistantRuntimeFacts`，只放在 namespaced run metadata。用户身份只来自 Agent Server
`Runtime.server_info.user.identity`。公开 context 的 `persona` 仅选择回复策略，不是认证身份或执行模式，不接受任意 prompt；`cwd` 是用户可见的
运行位置，不是隐藏身份或授权事实。

实时 VIDEO 的当前画面是瞬时事实；若受信入口暴露 `live_view_inspect`，每个新的指示性视觉问题都必须重新调用，不能把历史视觉
Tool observation 当作本轮证据。`visual_memory_search` 只查当前 VIDEO thread 的短期视觉时间线；Memory middleware 自动召回的
跨会话长期视觉文本不添加来源标签，直接合并进临时背景参考消息。

## Skill、filesystem 与 task state

Deep Agents `SkillsMiddleware` 从同一个 `CompositeBackend` 的两条虚拟 route 发现标准 `SKILL.md` L0 元数据：
`/source-skills/` 对应随源码发布的 `<source-root>/skills/`，`/cwd-skills/` 对应每次 run 的 `<cwd>/skills/`；后者同名
覆盖前者，且每次 run 重新发现，避免 thread 切换 cwd 后沿用旧目录。项目 state 与 Prompt 只投影 `name`、`description`、
`path`；上游可选的 `allowed_tools`、`compatibility`、`license`、`metadata` 不进入项目运行时。两条 route 与主/worker 的
working-directory backend 一起交给同一套 filesystem Tool，因此 Prompt 中的路径可直接由 `read_file` 按需读取；正文只在
实际读取后进入当前角色 transcript。Skills 不授予 Tool，也不扩大 task state。

同步 `task` 使用双向显式 allowlist，而不是传递整个 Deep Agent state：

- 父级到 worker：恰好一条任务 `HumanMessage`，以及存在时冻结的 `memory_context`；
- worker 到父级：最后一条非空 `AIMessage`，以及存在时的 `structured_response`。

Todo、`async_tasks`、Tool Profile、Skill metadata、文件读取 transcript 和未知未来字段都不会
跨越该边界。worker 内部 transcript 也不回灌父级。middleware 自有 channel 使用 `PrivateStateAttr`，包括
`ToolProfileMiddleware.active_tool_profile_ids` 和递归收尾的 `remaining_steps`；它们不是 task 或公开 context 合同。
`async_tasks` 同样使用 `PrivateStateAttr`，仅供业务 state/checkpoint 消费。业务图的 `memory_context` 使用
`OmitFromInput + OmitFromOutput`，仅通过 allowlist 传给 worker。
若 worker 既没有非空 `AIMessage`，也没有非空 structured response，输出投影会生成一条有界的明确失败报告，
不会用空 `AIMessage` 伪装成功；已有非空文本或 structured response 的投影语义不变。

异步 delegation 的父会话只持久化有界 task handle 和父子 thread/run correlation，不保存 Workspace 或 repository snapshot。
异步 worker 的业务输入仍只有模型生成的 description；
父 conversation、Todo、Skill/Profile state 与完整 Tool transcript 不自动复制。

## Memory、预算与历史

`before_agent` 冻结的 `memory_context` 不进入 system prompt。位于 summarization 内层的 model-call middleware 在最新真实
`HumanMessage` 前临时插入一条引用格式的背景参考 `HumanMessage`，明确它可能过时且禁止作为用户指令。这条消息不写入
state、checkpoint messages 或摘要，不能用于确认身份、权限、当前事实和操作参数。

dynamic prompt 只加入 `Asia/Shanghai` 的自然日和可信配置地区，不加入时分秒；本轮明确给出的任务地点优先，且
不会反向修改配置。Provider 联网来源只属于产生它的 `AIMessage.response_metadata`，不会变成下一轮上下文消息。
Tool observation 使用标准 `ToolMessage(content, artifact)`；runtime-owned 字段不进入模型可见 schema。

业务 Agent 与通用 worker 各只装配一套 Deep Agents summarization；该 model-call middleware 统计当前可见的 system message、
有效历史与 Tool schema，并通过 `_summarization_event` 保留原始 message log。被压缩的完整历史沿用 Deep Agents
`/conversation_history/session_<uuid>.md` 约定，实际写入本轮执行目录 `<cwd>/.agent/history`，有设备绑定时由客户端完成，不使用 thread artifact。
main/worker 在同一 model superstep 内每 Tool 最多并行 12 次，并在
`recursion_limit` 只剩 8 个 superstep 时关闭 Tool 生成自然综合。所有显式配置 Tool 的审批由 Runtime/Tool authority
统一定义，不通过上下文模式切换。

业务/worker summarization 的绝对 token trigger/keep 分别由同一 composition 投影的
`ChatConfig.context_input_token_limit * context_compaction_trigger_ratio/target_ratio` 计算，不写死模型窗口。
composition 启动时按主模型 ID 解析 API protocol、context window 与 tokenizer，并把同一个 `count_messages`、`context_window_tokens`、
`compaction_trigger_ratio` 和 `compaction_target_ratio` 投影同时传给 main 与 worker。real DeepSeek V4 或 native LLM compactor 缺少本地
tokenizer 时启动直接失败，不回退近似计数。已登记的托管模型 tokenizer 使用固定 revision 并由 `tokenizers` 缓存在本机；首次使用
允许从官方模型仓库获取，后续启动复用缓存。显式 `MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH` 仍可覆盖自动选择。
启用视觉时间线 LLM compactor 时同样复用该 counter 的
`count_text`，不再加载第二份视觉 tokenizer。

旧 `ContextService`、prompt-json compiler、独立 runtime system prompt policy、动态 catalog/exposure、renderer、
平行 compactor、旧 source/policy 与未接线 context report 已删除；视觉预算和实时视频 DTO 由视觉感知域就近维护。
生产 Agent Server/native graph 不保留平行 `context` package。

## 验证

```bash
python -m compileall -q src/assistant_agent/native_agent/context.py
```

以上仅检查语法；行为变更的临时测试按 `tests/README.md` 显式运行。
