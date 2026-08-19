# LangChain-native Context Engineering

最后更新：2026-08-18

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Agent 标准 messages、dynamic prompt、预算与 summarization 的当前权威 |
| Owns | dynamic system prompt、Memory 数据边界、标准 message history、官方 limit/summarization middleware |
| Does not own | Tool schema、Memory backend、Provider wire、媒体 frame、旧 ContextService |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/fast_agent.py`、`src/assistant_agent/native_agent/state.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification`；核心不变量 `CTX-001` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 当前主链

生产上下文以 LangChain 标准 `messages` channel 为事实源。fast agent 的 `dynamic_prompt` 只维护一份
provider-neutral 的自然语言操作策略，明确回答目标、Tool 使用条件、事实与推断边界、失败处理和 progressive
Skill 加载顺序，不按 Provider 复制模板，也不向模型暴露无行为价值的 runtime 枚举或信任标签。只有非空媒体
能力会被翻译成一句可执行的入口说明。

dynamic prompt 同时只渲染可发现 Skill 的 L0 index；完整 Skill 正文由 `load_skill` 读取，专项 reference 再由
`load_skill_reference` 按当前 fast agent 子图 state/checkpoint namespace 中的窄 grant 读取。
Skill 激活状态只在该次子图执行及其原生 interrupt/resume 中保存受信 `skill_id` 和注册的
reference ID，不进入父图、后续 chat run 或 Memory Graph；
模型可见 Tool schema 由原生 model-call middleware 根据 manifest 的 `governed_tools` 派生，
不把 Tool schema、任意 Tool 名或 Skill 正文复制进 checkpoint。

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

模型调用上限、Tool 调用上限、只读 Tool retry、长对话 summarization 与 planning 模式非 read Tool HITL
全部使用官方 middleware；fast 模式自动放行。summarization 默认采用输入窗口 75% 触发、保留 15% 的
token 阈值，两者可由现有环境变量覆盖。DeepSeek V4 Flash 使用其官方 tokenizer 与
`encoding_dsv4.py` 对标准 messages 做调用前计数；结构化 user content 只在文本 encoder 的计数副本中
提取 text block，原始多模态 message 与媒体引用保持不变。摘要读取被淘汰的完整消息前缀，不再应用默认
4K 局部裁剪。real 模式必须把 `MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH` 配置为本机官方快照中
`tokenizer.json` 的绝对路径，并在同级目录保留官方 `encoding/encoding_dsv4.py`；快照目录属于本机未跟踪
运行资产，不进入仓库。切点仍由官方 middleware 选择，只保护 `AIMessage(tool_calls)` 与对应
`ToolMessage`，不维护项目
自建 conversation、完整问答边界或 summary state。

planning worker 只获得自己的 objective、父图 Memory 与 TrustedRuntimeFacts 快照、调度器按 `depends_on` 派生的直接上游
`dependency_results` 和同一个 fast agent。planner 默认生成单节点最小计划，只在存在真实独立工作或直接依赖时
拆分，并要求 objective 自包含且保留相关用户约束。依赖结果不能覆盖当前任务、身份、权限或 Tool 约束。
worker transcript 不并入父图对话；父图只接收结构化 `WorkerResult`。finalize 使用同一个模型根据原始请求和按
plan 排序的 worker results 综合标准 `AIMessage`，显式处理冲突、缺失和失败，不把中间结果机械拼接成最终答案。
planner 与 finalizer 的直接模型调用也显式注入同一份 TrustedRuntimeFacts；子图不会自行读取系统时钟或地点。

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
