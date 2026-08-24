# LangChain-native Context Engineering

最后更新：2026-08-24

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

dynamic prompt 的 L0 index 只用于发现可加载 Skill。成功执行 `load_skill` 后，Tool 自身按 LangGraph 原生
state-update 契约返回包含标准 `ToolMessage` 的 `Command(update=...)`，把受信 `skill_id` 写入当前 fast agent
子图的 `active_skill_ids`；每次后续 model call 都以该 ID 从 composition 注入的受信 catalog 重新取得并把完整
Skill 正文追加到 system prompt。`load_skill` 的模型观察与 artifact 只返回指导、Skill/reference 标识和加载状态，
不返回 Tool capability。独立 exposure middleware 只在共享 fast/Worker `create_agent` 的原生 model-call hook 中
根据 manifest 的 `governed_tools` 与当前 `active_skill_ids` 派生业务 Tool schema；planning Worker 另由窄
middleware 隐藏 Skill control Tool，Supervisor 自己只绑定 control 与 task schema。专项 reference 再由
`load_skill_reference` 按当前 state/checkpoint namespace 中的窄 grant 读取。checkpoint 只保存受信 Skill ID 和
注册的 reference ID，不复制
Skill 正文、Tool schema 或任意 Tool 名；这些状态不进入父图、后续 chat run 或 Memory Graph。

Skill L0 index 使用简短自然语言列表。父图冻结的 `memory_context` 与 `trusted_runtime_facts` 都不进入
system prompt：位于 summarization 内层的 model-call middleware 在最新真实 `HumanMessage` 前分别临时插入
两条独立 `HumanMessage`。模型请求中的尾部顺序固定为 MemoryContext、TrustedRuntimeFacts、当前真实用户请求；
前面的静态 system prompt 与持久历史保持稳定，以利于 Provider KV prefix cache。两条临时消息都不写入标准
messages state、checkpoint messages 或摘要；结构化事实快照本身由父图 state/checkpoint 保存。

Memory 每一行以引用文本呈现，并明确为可能过时或错误的背景资料而非本轮指令；不能用于生成身份、权限、当前
事实和 Tool 参数。TrustedRuntimeFacts 提供带时区的采集时间和部署默认地点；当前默认地点为
“上海市青浦区华为练秋湖研发中心”。结构化快照仍标记 `source=deployment_default`、`is_fallback=true`，
模型可见临时消息使用“用户默认地点”中文字段，并明确该地点不是已观测用户物理位置。用户在当前请求中明确指定的任务地点可以
覆盖本次任务参数，但不能改写可信事实的来源。最后一条用户消息始终是本轮真实请求。

共享 fast/Worker `create_agent` 使用官方 `ModelCallLimitMiddleware` 与 `ToolCallLimitMiddleware` 提供每次 invocation
的 model/tool call 安全上限，不把 usage 或 reservation 写入 planning 父 state。只读 Tool retry、长对话
summarization 与 planning 模式非 read Tool HITL 继续使用官方
middleware；需要独立 run 上限的 Tool 由同一个 metadata-driven per-Tool limiter 分别计数，不是全 Tool global
limiter；当前 live-view 声明每个 run 最多一次，fast agent 不识别具体 Tool 名。fast 模式自动放行，planning
Worker 的非 read 业务 Tool 在执行前 interrupt，并从原生 checkpoint approve/resume，不重放已完成分支。
Supervisor 的三个 control Tool 都是 read。summarization 默认采用输入窗口 75% 触发、保留 15% 的
token 阈值，两者可由现有环境变量覆盖。DeepSeek V4 Flash 使用其官方 tokenizer 与
`encoding_dsv4.py` 对标准 messages 做调用前计数；结构化 user content 只在文本 encoder 的计数副本中
提取 text block，原始多模态 message 与媒体引用保持不变。摘要读取被淘汰的完整消息前缀，不再应用默认
4K 局部裁剪。real 模式必须把 `MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH` 配置为本机官方快照中
`tokenizer.json` 的绝对路径，并在同级目录保留官方 `encoding/encoding_dsv4.py`；快照目录属于本机未跟踪
运行资产，不进入仓库。切点仍由官方 middleware 选择，只保护 `AIMessage(tool_calls)` 与对应
`ToolMessage`，不维护项目
自建 conversation、完整问答边界或 summary state。

planning Supervisor 是普通 LLM node，不使用 `create_agent`。每次调用由独立 prompt 构造函数重新生成
`SystemMessage`；其正文直接来自锁定依赖 `langchain==1.3.15` 的
模块级常量 `langchain.agents.middleware.todo.WRITE_TODOS_SYSTEM_PROMPT`，生产代码直接导入该常量而不在仓库复制
prompt 正文；来源固定到
[`langchain==1.3.15/todo.py`](https://github.com/langchain-ai/langchain/blob/langchain%3D%3D1.3.15/libs/langchain_v1/langchain/agents/middleware/todo.py#L119-L136)。
该上游原文要求模型自行标记 completed，与 A-lite“只有 join 能完成 Todo”的状态契约存在已知语义冲突；本次按用户明确选择
保留原文、不添加项目修订，确定性状态校验仍拒绝 Supervisor 直接完成 Todo。

Supervisor 每次调用先读取经官方 token-aware trimming 的父自然对话，再在最新真实用户请求前临时插入四类上下文，
顺序固定为：planning working memory、MemoryContext、TrustedRuntimeFacts、最新真实 `HumanMessage`。planning working
memory 本身也是独立 `HumanMessage`，只包含 Todo、当前 Worker result、从受信 catalog 机械重读的 active Skill 正文与
已成功授权并重读的 reference 正文，以及可发现 L0 Skill catalog 的 `skill_id/description`。planning working-memory
消息带有固定 `name` 和 `additional_kwargs` 来源标识，使离线 mock 及其他受信消费者不会把用户提交的同形 JSON 当作
内部 working memory；Memory 与 TrustedRuntimeFacts 各自使用 fast/Worker 相同的安全文案和独立
`HumanMessage`。这些临时消息均不写入父 `messages`、checkpoint messages 或摘要，最后一条消息始终是本轮真实用户请求。
planning control/task transcript 仍由 checkpoint 保存，但不重复投影给 Supervisor；无 ToolCall 时直接形成标准终态
`AIMessage`。Todo 是 working memory，不是依赖或授权协议。

每个 planning Worker 通过 `agent_phase="worker"` 复用同一个 `AssistantFastAgent`，但只获得当前 Todo、已加载的
必要 Skill/reference、父图冻结的 Memory 与 TrustedRuntimeFacts，以及一条新建的私有 `HumanMessage`。完整父
conversation、其他 Todo 和 Worker 内部业务 Tool transcript 不进入该私有消息。Worker 只返回严格
`WorkerResult(todo_id,status,summary)`；join 只把受控结果作为原 `task` call 对应的父级 `ToolMessage` 写回。
只有 join 能根据 `succeeded` result 把 Todo 标成 completed；`write_todos` 只能维护 pending，改写 pending 内容时
同步清除同 ID 的旧 blocked result。`succeeded` result 与 completed Todo 单调保护，`blocked` 保持 pending 并由 Supervisor 决定 retry/replan/finish；
operational exception 不转换为业务结果，由 LangGraph pending writes/resume 恢复。

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
  tests/tdd/native-agent-parent-graph/test_fast_agent.py
```
