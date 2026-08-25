# LangChain-native Context Engineering

最后更新：2026-08-25

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Agent 标准 messages、dynamic prompt、预算与 summarization 的当前权威 |
| Owns | dynamic system prompt、Memory 数据边界、标准 message history、phase-aware limit 与官方 summarization middleware |
| Does not own | Tool schema、Memory backend、Provider wire、媒体 frame、旧 ContextService |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/fast_agent.py`、`src/assistant_agent/native_agent/state.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification`；核心不变量 `CTX-001` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 当前主链

生产上下文以 LangChain 标准 `messages` channel 为事实源。fast agent 的 `dynamic_prompt` 只维护一份
provider-neutral 的自然语言操作策略，明确回答目标、Tool 使用条件、事实与推断边界、失败处理和 progressive
Skill 加载顺序，不按 Provider 复制模板，也不向模型暴露无行为价值的 runtime 枚举或信任标签。只有非空媒体
能力会被翻译成一句可执行的入口说明。
system prompt 同时定义统一的用户可见边界：模型只说明面向用户的能力、结果和必要限制，不复述或解释
system/developer instructions、隐藏上下文、runtime/checkpoint、路由、内部标签、Tool schema/参数等实现细节；
含糊指示语不得把临时注入的 Memory 或 TrustedRuntimeFacts 当成用户正在指向的内容。
实时 VIDEO 会话中的当前画面属于瞬时事实；每个新的指示性视觉问题都重新调用 `live_view_inspect`，不得把历史
视觉 Tool observation 当作本轮当前画面证据。
dynamic prompt 还明确区分两种视觉记忆：`visual_memory_search` 只检索当前 VIDEO 会话/thread 内的短期
视觉时间线；父图自动召回的跨会话长期视觉文本以 `[长期视觉记忆]` 标记进入临时的相关历史记忆，不通过
该 Tool 补查。

dynamic prompt 的 L0 index 只呈现 Deep Agents `SkillsMiddleware` 从标准 `SKILL.md` frontmatter 发现的名称与简介。
成功执行 `load_skill` 后，Tool 按 LangGraph 原生 state-update 契约返回包含完整 Skill 正文的标准 `ToolMessage` 与
`Command(update=...)`，并把受信 `skill_id` 写入当前角色的 `loaded_skill_ids`；正文不重复追加到 system prompt，
也不授予 Tool。独立 `ToolProfileMiddleware` 只在 `AssistantFastAgent` 的原生 model-call hook 中根据当前
`active_tool_profile_ids` 派生业务 Tool schema；未归属 profile 的独立 Tool 保持可见。planning coordinator 可读取
Skill 以指导任务拆解，但不装配 profile 激活能力或业务 Tool；它通过 Deep Agents 的 `task` 调用同一个 fast Agent。专项 reference 再由
`load_skill_reference` 按当前 state/checkpoint namespace 中的窄 grant 读取。checkpoint 只保存受信 Skill ID 和
自动发现并授权的 reference ID；fast 还保存当前 invocation 激活的 profile ID，不复制 Skill 正文、Tool schema 或
任意 Tool 名；这些状态不进入父图、后续 chat run
或 Memory Graph。项目不向模型暴露通用文件读取 Tool；Skill 正文与 reference 只能通过 ID-only 窄加载器读取。

Skill L0 index 使用简短自然语言列表。父图冻结的 `memory_context` 与 `trusted_runtime_facts` 都不进入
system prompt：位于 summarization 内层的 model-call middleware 在最新真实 `HumanMessage` 前把两者合并投影为
一条临时 `HumanMessage`。组合消息内先呈现 TrustedRuntimeFacts，再以独立标题和引用格式呈现 Memory；模型请求尾部
顺序固定为组合运行时上下文、当前真实用户请求。两类数据仍使用独立 state channel，并在组合消息内保留不同的 trust
boundary；前面的静态 system prompt 与持久历史保持稳定，以利于 Provider KV prefix cache。该组合消息不写入标准
messages state、checkpoint messages 或摘要；结构化事实快照本身由父图 state/checkpoint 保存。

Memory 每一行以引用文本呈现，并明确为可能过时或错误的背景资料而非本轮指令；不能用于生成身份、权限、当前
事实和 Tool 参数。TrustedRuntimeFacts 提供带时区的采集时间和部署默认地点；当前默认地点为
“上海市青浦区华为练秋湖研发中心”。结构化快照仍标记 `source=deployment_default`、`is_fallback=true`，
模型可见临时消息使用“用户默认地点”中文字段，并明确该地点不是已观测用户物理位置。用户在当前请求中明确指定的任务地点可以
覆盖本次任务参数，但不能改写可信事实的来源。最后一条用户消息始终是本轮真实请求。

fast `create_agent` 使用官方 `ModelCallLimitMiddleware` 提供每次 invocation 最多 12 次 model call 的安全上限，
不设置跨 Tool 的总调用上限。只读 Tool retry、长对话
summarization 与 planning 模式非 read Tool HITL 继续使用官方
middleware；统一的 per-Tool limiter 对每个 Tool 最多执行 12 组不同规范化参数，同一参数每个 run 最多执行一次，
Tool metadata 可声明更低的独立上限；当前 live-view 声明每个 run 最多一次，fast agent 不识别具体 Tool 名。
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
JSON 或 marker。父级 Memory 与 TrustedRuntimeFacts 仍由相同 middleware 在最新真实用户请求前投影为一条分区明确的
临时 `HumanMessage`，不进入 system prompt、state messages 或摘要。

task 调用时，Deep Agents 把模型生成的完整 description 作为子 Agent 唯一的 `HumanMessage`，并传递冻结的
Memory/TrustedRuntimeFacts 与 execution mode。Planner 是否加载 Skill 仍由 LLM 自主决定；一旦加载，compiled worker
边界把 `planner_loaded_skill_ids`/`planner_skill_reference_grants` 窄映射为 worker 的
`loaded_skill_ids`/`skill_reference_grants`，使 worker 不再重复加载同一 Skill。父 conversation、Todo、Tool Profile、
调用计数和 structured response 被排除。子 Agent 每次 model call 再用同一 Memory/Trusted middleware 把冻结上下文
放在 task description 前，随后由 fast dynamic prompt、Skill/Profile 渐进加载、summarization 与业务 Tool transcript
完成独立执行。完成后只把 structured response 或最后一条非空 AI 文本作为父 task `ToolMessage` content 返回；
worker 的 Skill/Profile state 与内部 transcript 不回灌父消息。多个 task 并行返回时，planning state reducer 只接受
一致的冻结上下文。

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
