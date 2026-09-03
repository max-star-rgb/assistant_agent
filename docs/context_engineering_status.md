# LangChain-native Context Engineering

最后更新：2026-09-02

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 统一生产 Agent 的标准 messages、dynamic prompt、state 投影、预算与 summarization 权威 |
| Owns | dynamic system prompt、Memory 数据边界、标准 message history、task state allowlist 与官方 summarization middleware |
| Does not own | Tool schema、Memory backend、Provider wire、媒体 frame、旧 ContextService |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_prompt.py`、`native_agent/assistant_agent.py`、`native_agent/user_context.py`、`native_agent/context.py`、`native_agent/state.py`、`native_agent/token_counter.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification`；核心不变量 `CTX-001` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 当前主链

生产上下文以 LangChain 标准 `messages` channel 为事实源。统一 `AssistantAgent` 使用一个分层 Prompt Builder：
provider-neutral 的稳定核心策略在前，Deep Agents Skills L0 目录随后注入，dynamic prompt 最后追加当前工作目录、
项目指令、北京时间自然日、可信用户地区与本轮媒体事实。当前工作目录来自公开 `AssistantRunContext.cwd`，默认是
当前 OS 用户 Home，必须解析为 Home 内已存在目录。项目指令只沿 Home 到 cwd 的祖先链读取；同层
`AGENTS.override.md` 优先于 `AGENTS.md`，单次最多注入 32 KiB，不扫描 Home 的其他目录。
预算不足时优先保留最接近 cwd 的指令，再以祖先到子目录的顺序呈现；自动加载拒绝解析到 Home 外的
指令文件 symlink，避免把 cwd 选择扩大为隐式文件读取授权。最终 runtime dynamic prompt 将上游 middleware
生成的普通纯文本 section 合并为单个 text content block；带额外元数据或非文本 block 保持原结构。

稳定策略要求直接推进结果，只在缺少阻塞信息或关键选择会改变结果时询问，并区分
已核验事实、失败和模型判断。Git 仓库识别与命令执行由 `git` Tool 的代码边界负责，不写入 system prompt。

公开 `AssistantRunContext` 包含 `cwd`、`enable_memory`、`require_tool_approval`，以及 Studio 可配置的成对绝对值
`context_compaction_trigger_tokens` / `context_compaction_keep_tokens`。压缩值都不设置时使用 Provider context window 的
75%/15%，设置时必须满足 `0 < keep < trigger`，并由 main/worker 的 Deep Agents
`SummarizationMiddleware.awrap_model_call` 在当前 run 中读取而不修改进程共享 middleware。
后两个布尔值默认均为 true，`enable_memory` 控制本轮 recall 与 delayed extraction，`require_tool_approval` 允许单次 run
关闭已配置 Tool 的 HITL interrupt，两者都不进入 prompt。`entry_profile` 与视觉 capability token 属于服务端签发的
`AssistantRuntimeFacts`，只放在 namespaced run metadata。用户身份只来自 Agent Server
`Runtime.server_info.user.identity`。公开 context 不包含模式、任意 prompt、身份、仓库注册或 Tool 授权；`cwd` 是用户可见的
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
`ToolProfileMiddleware.active_tool_profile_ids`、递归收尾的 `remaining_steps`，以及主 loop 的
`needs_verification` / `verification_attempts`；它们不是 task 或公开 context 合同。
`async_tasks` 同样使用 `PrivateStateAttr`，仅供当前 state/checkpoint 流程消费。`memory_context` 需经上述
allowlist 传给 `general-purpose` worker，因此使用
`OmitFromInput + OmitFromOutput`。两种原生 metadata 都保证这些字段不进入生产 Graph 公开 input/output。
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

统一 Agent 与通用 worker 都只装配一套 Deep Agents summarization；该 model-call middleware 统计当前可见的 system message、
有效历史与 Tool schema，并通过 `_summarization_event` 保留原始 message log。被压缩的完整历史沿用 Deep Agents
`/conversation_history/session_<uuid>.md` 约定，实际写入动态 `Path.home()`，不依赖本轮 `cwd` 或 thread artifact。
main/worker 在同一 model superstep 内每 Tool 最多并行 12 次，并在
`recursion_limit` 只剩 8 个 superstep 时关闭 Tool 生成自然综合。所有显式配置 Tool 的审批由 Runtime/Tool authority
统一定义，不通过上下文模式切换。

summarization 的绝对 token trigger/keep 分别由同一 composition 投影的
`ChatConfig.context_input_token_limit * context_compaction_trigger_ratio/target_ratio` 计算，不写死模型窗口。
composition 启动时先创建配置的离线 token counter，并把同一个 `count_messages`、`context_window_tokens`、
`compaction_trigger_ratio` 和 `compaction_target_ratio` 投影同时传给 main 与 worker。real DeepSeek V4 或 native LLM compactor 缺少本地
tokenizer 时启动直接失败，不回退近似计数或发起网络调用。启用视觉时间线 LLM compactor 时同样复用该 counter 的
`count_text`，不再加载第二份视觉 tokenizer。

旧 `ContextService`、prompt-json compiler、独立 runtime system prompt policy、动态 catalog/exposure、renderer、
平行 compactor、旧 source/policy 与未接线 context report 已删除；视觉预算和实时视频 DTO 由视觉感知域就近维护。
生产 Agent Server/native graph 不保留平行 `context` package。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_context_lifecycle.py
```
