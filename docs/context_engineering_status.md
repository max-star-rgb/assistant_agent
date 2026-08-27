# LangChain-native Context Engineering

最后更新：2026-08-26

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Agent 标准 messages、dynamic prompt、预算与 summarization 的当前权威 |
| Owns | dynamic system prompt、Memory 数据边界、标准 message history、phase-aware limit 与官方 summarization middleware |
| Does not own | Tool schema、Memory backend、Provider wire、媒体 frame、旧 ContextService |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_prompt.py`、`src/assistant_agent/native_agent/fast_agent.py`、`src/assistant_agent/native_agent/user_context.py`、`src/assistant_agent/native_agent/context.py`、`src/assistant_agent/native_agent/state.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification`；核心不变量 `CTX-001` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 当前主链

生产上下文以 LangChain 标准 `messages` channel 为事实源。fast 与 planning 共用分层 Prompt Builder：第一层是
provider-neutral、不可由 Assistant 覆盖的稳定核心策略，采用紧凑的任务、回复与安全三段结果契约。
它要求直接推进到结果，只在信息阻塞或关键选择会改变结果时询问；公共或可变事实、用户私有数据和实际动作
使用当前可用的工具或联网能力核验，并区分已核验事实、失败和模型判断。Tool Profile 目录与激活语义只由 Tool description
提供，Skill L0 与临时 Memory 的使用边界只保留在各自的动态注入中，不在稳定核心重复。
SkillsMiddleware 在稳定核心之后追加 L0 目录；最后的 dynamic prompt 后缀按 `## 用户信息`、
`## 当前媒体上下文` 的顺序追加更易变的信息。Studio Assistant 的公开 runtime context 只包含 execution preset 与
`enable_memory`，后者统一控制单次 run 的 recall 和 delayed extraction，不进入 prompt。该次序使稳定前缀优先复用，
同时避免把入口枚举、capability、prompt 或仓库选择暴露为 Assistant 配置。
fast 不追加 mode-specific system prompt；planning 由 `TodoListMiddleware` 追加自己的 Todo system prompt，
`task` 委派规则只保留在 SubAgent Tool description 中。共享核心不接收 Tool Profile 或其他 mode-specific 参数。
system prompt 同时定义统一的用户可见边界：模型只说明面向用户的能力、结果和必要限制，不复述或解释
system/developer instructions、隐藏上下文、runtime/checkpoint、路由、内部标签、Tool schema/参数等实现细节；
含糊指示语不得把临时注入的 Memory 或动态用户信息当成用户正在指向的内容。
实时 VIDEO 会话中的当前画面属于瞬时事实；每个新的指示性视觉问题都重新调用 `live_view_inspect`，不得把历史
视觉 Tool observation 当作本轮当前画面证据。
dynamic prompt 还明确区分两种视觉记忆：`visual_memory_search` 只检索当前 VIDEO 会话/thread 内的短期
视觉时间线；父图自动召回的跨会话长期视觉文本以 `[长期视觉记忆]` 标记进入临时的相关历史记忆，不通过
该 Tool 补查。

Deep Agents `SkillsMiddleware.before_agent` 从标准 `SKILL.md` frontmatter 发现名称与简介，并由同一 middleware 从
runtime `skills_metadata` 向 system message 注入唯一 L0 目录。目录使用 `SkillsMiddleware` 原生 `system_prompt` 插槽和
项目精简模板，只保留明确命中后读取、supporting file 路由、Skill/Tool Profile 分离及不泄漏内部流程的说明；不注入
通用脚本执行教程、无关 workflow 示例或 “when in doubt” 式过度加载建议。模型通过将 `/skills/` 单独绑定当前项目 `skills/` 的上游
`FilesystemMiddleware.read_file` 读取完整正文与 supporting files；读取结果只进入当前角色 transcript，不维护
`loaded_skill_ids` 或 reference grant，也不授予 Tool。独立 `ToolProfileMiddleware` 在 fast 的原生 model-call hook 中根据当前
`active_tool_profile_ids` 派生业务和 `async-tasks` Tool schema；未归属 profile 的独立 Tool 保持可见。planning coordinator 可读取
Skill 以指导任务拆解，只装配 `async-tasks` Profile 而不装配业务 Tool，并通过 Deep Agents 的 `task` 调用同一个 fast Agent。
`read_file` 不进入业务 Tool inventory；文件 Tool 的默认虚拟根映射当前项目，显式 `/home/lenovo1/...` 路径可访问用户主目录，但不能越过该目录访问系统其他路径。fast 只保存当前 invocation 激活的
profile ID，不复制 Skill 正文、Tool schema 或任意 Tool 名到父图、后续 chat run 或 Memory Graph。

Skill L0 index 使用简短自然语言列表。父图冻结的 `memory_context` 不进入 system prompt：位于 summarization 内层的
model-call middleware 只在最新真实 `HumanMessage` 前投影一条临时 Memory `HumanMessage`。Memory 每一行使用引用格式，
并明确为可能过时或错误的背景资料而非本轮指令；不能用于确认身份、权限、当前事实和 Tool 参数。该临时消息不写入
标准 messages state、checkpoint messages 或摘要，最后一条用户消息始终是本轮真实请求。

fast 与 planning 的 model-call middleware 最后使用 LangChain 原生 `dynamic_prompt` 追加易变 system prompt 后缀。
后缀只包含北京时间自然日（`YYYY-MM-DD`）与可信配置提供的用户地区；不包含时分秒。地区默认使用
“上海市青浦区华为练秋湖研发中心”，`MULTIMODAL_AGENT_CURRENT_LOCATION` 可显式覆盖；进入 prompt 前会折叠为单行文本。
日期统一按 `Asia/Shanghai` 计算。两项事实置于与 `## Skills` 平级的 `## 用户信息` 区块，并与前一个 system text block 用空行分隔。
若当前消息带用户上传媒体或媒体入口投影的实时视频引用，同一后缀再加入对应的可执行视觉说明；媒体能力不由
`AssistantRunContext` 声明。该后缀位于稳定核心和 Assistant 指令之后，因此日期通常每天只改变一次，前面的长公共前缀仍可命中 Provider
KV prefix cache。日期和地区不进入 Graph state/checkpoint；当前请求明确指定的任务地点仍优先作为该任务参数，不会反向
改写用户配置。

fast、planning 与 coding 不设置 model call 次数或单个 Tool 的 run 累计次数上限。它们使用 LangGraph 原生
`RemainingSteps` 在 `recursion_limit` 只剩 8 个 superstep 时关闭 Tool 并生成自然综合答复。只读 Tool retry、长对话
summarization 与 planning 模式非 read Tool HITL 继续使用官方
middleware；统一的 per-Tool limiter 在同一 model superstep 内允许同名 Tool 最多并行调用 12 个实例，后续 turn
可再次调用任意参数。项目不保存跨轮参数 fingerprint。
fast 模式自动放行，planning
task 内的 fast 子 Agent 对非 read 业务 Tool 在执行前 interrupt，并从原生 checkpoint approve/resume。
planning coordinator 自身也使用相同的 model/per-Tool 参数级 limit 与 summarization。summarization 默认采用输入窗口 75% 触发、保留 15% 的
token 阈值，两者可由现有环境变量覆盖。DeepSeek V4 Flash 使用其官方 tokenizer 与
`encoding_dsv4.py` 对标准 messages 做调用前计数；结构化 user content 只在文本 encoder 的计数副本中
提取 text block，原始多模态 message 与媒体引用保持不变。摘要读取被淘汰的完整消息前缀，不再应用默认
4K 局部裁剪。real 模式必须把 `MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH` 配置为本机官方快照中
`tokenizer.json` 的绝对路径，并在同级目录保留官方 `encoding/encoding_dsv4.py`；快照目录属于本机未跟踪
运行资产，不进入仓库。切点仍由官方 middleware 选择，只保护 `AIMessage(tool_calls)` 与对应
`ToolMessage`，不维护项目
自建 conversation、完整问答边界或 summary state。

planning coordinator 本身也是 `create_agent`。锁定依赖 `langchain==1.3.15` 的官方
`TodoListMiddleware` 在每次 model call 动态追加项目通过官方扩展参数提供的中文 system prompt，并提供原生
`write_todos` Tool；项目同样通过官方扩展参数提供中文 Tool description，不复制或改写 middleware 执行逻辑，
也不再叠加项目 A-lite completed 规则。Todo 采用上游
`content/status=pending|in_progress|completed` schema，由模型通过官方 Tool 实时维护。

Deep Agents 0.7.8 `SubAgentMiddleware` 提供原生 `task(description, subagent_type)` Tool；唯一
`general-purpose` 类型直接引用已编译的 `AssistantFastAgent`。Supervisor 的标准 messages、Todo ToolCall、task
ToolCall 与返回的 ToolMessage 全部保留在官方 conversation/checkpoint 中，不再经过项目 projection、working-memory
JSON 或 marker。父级 Memory 仍由相同 middleware 在最新真实用户请求前投影为一条临时 `HumanMessage`，不进入
system prompt、state messages 或摘要；日期与地区由 coordinator 自己的 dynamic system prompt 提供。

task 调用时，Deep Agents 把模型生成的完整 description 作为子 Agent 唯一的 `HumanMessage`，并传递冻结的
Memory 与 execution mode。Planner 是否读取 Skill 仍由 LLM 自主决定，并把与 task 相关的规则写入
description；Skill metadata、读取 transcript 和加载状态不跨 Agent 传递。父 conversation、Todo、Tool Profile、调用计数
和 structured response 被排除。子 Agent 每次 model call 再用 Memory middleware 把冻结上下文放在 task
description 前，并由自己的 dynamic prompt 取得当日日期与配置地区；随后由上游 Skills/read_file、Tool Profile、summarization 与业务 Tool transcript
完成独立执行。完成后只把 structured response 或最后一条非空 AI 文本作为父 task `ToolMessage` content 返回；
worker 的 Skill/Profile state 与内部 transcript 不回灌父消息。多个 task 并行返回时，planning state reducer 只接受
一致的冻结上下文。

Deep Agents `AsyncSubAgentMiddleware` 另向 fast 与 planning coordinator 静态注册五个后台任务 Tool；五者归入独立
`async-tasks` Profile，激活前不向模型暴露，激活后才在当前 invocation 中可见。父会话只持久化
有界 `async_tasks` handle（task/thread/run ID、状态与时间戳），不复制 worker conversation、Tool transcript 或私有 state；
handle 位于三种共享 state schema 中，因此切换 fast/planning 后仍可管理。worker 收到的唯一业务输入是模型生成的
description，身份通过 Agent Server auth 单独绑定，父 Memory、Todo、Skill/Profile state 和完整 conversation 不自动复制。
同步 `task` 复用的 fast agent 若启动后台任务，只允许把新 task handle 经同一 `async_tasks` reducer 回传；其他私有 state
仍不回灌。

Provider 联网来源属于产生该回复的 `AIMessage.response_metadata`，不会作为新上下文消息重新注入后续模型调用。
终态入口只读取最新最终 AIMessage 的来源；中间 tool-call 或历史 AIMessage 的来源不聚合到当前答案。

Tool observation 由标准 `ToolMessage` 表达，结构化 artifact 保留在其 artifact 字段。模型可见 Tool schema
由 LangChain 生成；runtime-owned 身份字段不会进入 schema。Provider 的最终 token 准入由模型窗口配置和官方
middleware处理，局部媒体/文件 adapter 仍必须先执行自己的字节、路径与敏感信息限制。

旧 `ContextService`、prompt-json compiler、动态 catalog/exposure 与 renderer 已删除。仍保留的 context
代码只服务明确的离线报告、媒体压缩或中立 token/model DTO；生产 Agent Server/native graph 不导入平行
context runtime。后续外围迁移应复用标准 messages/middleware。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_context_lifecycle.py \
  tests/tdd/native-deepagents-planning
```
