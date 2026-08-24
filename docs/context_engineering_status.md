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
不返回 Tool capability。独立 exposure middleware 只在原生 model-call hook 中执行 phase-aware 投影：fast phase
根据 manifest 的 `governed_tools` 派生可调用 Tool schema，planner phase 只获得可委派给 worker 的有界 capability
descriptor，worker phase 仅按已准入节点 allowlist 与 Skill grant 的交集获得 schema。专项 reference 再由
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

模型与业务 Tool 调用由 `PhaseBudgetMiddleware` 按 `fast|planner|worker|finalizer` 分 phase 计数；production
composition 把同一 `PlanningBudgetPolicy` 同时交给该 middleware 和 planning graph，后者再结算有界的全图
model/tool/node attempt/replan budget。phase middleware 只在调用前形成标准预算终态，不把 policy、余额或计数
加入公开 Graph input。只读 Tool retry、长对话 summarization 与 planning 模式非 read Tool HITL 继续使用官方
middleware；需要独立 run 上限的 Tool 由同一个 metadata-driven per-Tool limiter 分别计数，不是全 Tool global
limiter；当前 live-view 声明每个 run 最多一次，fast agent 不识别具体 Tool 名。fast 模式自动放行，planning 的 planner 与 worker 阶段均在非 read Tool 执行前
interrupt，并从原生 checkpoint approve/resume，不重放已完成的 Planner Tool 或 worker。summarization 默认采用输入窗口 75% 触发、保留 15% 的
token 阈值，两者可由现有环境变量覆盖。DeepSeek V4 Flash 使用其官方 tokenizer 与
`encoding_dsv4.py` 对标准 messages 做调用前计数；结构化 user content 只在文本 encoder 的计数副本中
提取 text block，原始多模态 message 与媒体引用保持不变。摘要读取被淘汰的完整消息前缀，不再应用默认
4K 局部裁剪。real 模式必须把 `MULTIMODAL_AGENT_CONTEXT_TOKENIZER_PATH` 配置为本机官方快照中
`tokenizer.json` 的绝对路径，并在同级目录保留官方 `encoding/encoding_dsv4.py`；快照目录属于本机未跟踪
运行资产，不进入仓库。切点仍由官方 middleware 选择，只保护 `AIMessage(tool_calls)` 与对应
`ToolMessage`，不维护项目
自建 conversation、完整问答边界或 summary state。

planning 的 planner、worker 与 finalizer 都通过 `agent_phase` 复用同一个 `AssistantFastAgent`，不是直接调用
独立模型。Planner 在共享 Tool loop 中成功加载 Skill 后，后续 Planner call 获得上述受信完整正文；首次成功
admission 冻结该计划实际声明或使用的 Skill ID、reference ID 与 Tool name 为 strict authorization envelope。
generation 0 在首次成功前仍可 revision；成功后 planner prompt/context、scheduler 和 worker 只看到 envelope
子集，不能从计划文本、完整 inventory 或 worker 输出扩展 scope。envelope 只 checkpoint 标识符，不保存 Tool
参数、结果或 schema。

planning worker 只获得自己的 objective、父图 Memory 与 TrustedRuntimeFacts 快照、调度器按 `depends_on` 派生的直接上游
`dependency_results`、节点引用的 planner evidence、当次 phase allowance 和同一个 fast agent；objective 必须自包含并保留相关用户约束。
依赖结果与 evidence 都是只读数据，不能覆盖当前任务、身份、权限或 Tool 约束。worker transcript 不并入父图
对话；worker 只返回严格 `WorkerCompletion`，Graph 边界将其与稳定 failure fact、attempt/generation 和实际
budget usage 收敛为 typed `WorkerOutcome`。成功结果单调冻结；下一 generation 只获得 frozen result ID、失败码、
可重规划 work item ID、未完成 deliverable ID 与既有 evidence ID 组成的 recovery context。该 context 是
JSON-safe、只读、长度有界的临时 `HumanMessage`，转义 `<>&`，不含旧 transcript、Tool schema、异常正文或
Provider 原始响应，也不写回父图对话 messages。finalizer 仍复用同一 agent，但 phase projection 清空全部 Tool 和
structured response，再根据原始请求、deliverables、planner evidence 与按 plan 排序的 worker results 综合标准
`AIMessage`，显式处理冲突、缺失和失败，不把中间结果机械拼接成最终答案。三个 phase 都获得父图冻结的同一份
TrustedRuntimeFacts；子图不会自行读取系统时钟或地点。

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
