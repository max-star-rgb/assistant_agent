# LangChain-native Context Engineering

最后更新：2026-08-28

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 统一生产 Agent 的标准 messages、dynamic prompt、state 投影、预算与 summarization 权威 |
| Owns | dynamic system prompt、Memory 数据边界、标准 message history、task state allowlist 与官方 summarization middleware |
| Does not own | Tool schema、Memory backend、Provider wire、媒体 frame、旧 ContextService |
| 源码与 schema 入口 | `src/assistant_agent/native_agent/assistant_prompt.py`、`native_agent/assistant_agent.py`、`native_agent/user_context.py`、`native_agent/context.py`、`native_agent/state.py` |
| 验证入口 | `docs/authority.toml` 中 `context-engineering.verification`；核心不变量 `CTX-001` |
| 相邻 authority | Runtime 见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；Memory 见 [`memory-service-architecture.md`](memory-service-architecture.md) |

## 当前主链

生产上下文以 LangChain 标准 `messages` channel 为事实源。统一 `AssistantAgent` 使用一个分层 Prompt Builder：
provider-neutral 的稳定核心策略在前，Deep Agents Skills L0 目录随后注入，dynamic prompt 最后追加北京时间自然日、
可信用户地区与本轮媒体事实。稳定策略要求直接推进结果，只在缺少阻塞信息或关键选择会改变结果时询问，并区分
已核验事实、失败和模型判断。

公开 `AssistantRunContext` 只有 `enable_memory`，默认 true；它控制本轮 recall 与 delayed extraction，但不进入
prompt。`entry_profile`、视觉 capability token 与 repository snapshot SHA 属于服务端签发的
`AssistantRuntimeFacts`，只放在 namespaced run metadata。用户身份只来自 Agent Server
`Runtime.server_info.user.identity`。公开 context 不包含模式、prompt、身份、仓库选择或 Tool 授权。

实时 VIDEO 的当前画面是瞬时事实；若受信入口暴露 `live_view_inspect`，每个新的指示性视觉问题都必须重新调用，不能把历史视觉
Tool observation 当作本轮证据。`visual_memory_search` 只查当前 VIDEO thread 的短期视觉时间线；Memory middleware 自动召回的
跨会话长期视觉文本以 `[长期视觉记忆]` 标记进入临时 Memory message。

## Skill、filesystem 与 task state

Deep Agents `SkillsMiddleware` 从独立的普通 `FilesystemBackend` 的 `/skills/` 发现标准 `SKILL.md` L0 元数据，
项目 state 与 Prompt 只投影 `name`、`description`、`path`；上游可选的 `allowed_tools`、`compatibility`、`license`、
`metadata` 不进入项目运行时。正文只在模型实际调用 `read_file` 时进入当前角色 transcript。这个 backend 与主 Agent 的可写 worktree
backend、worker 的只读 worktree backend 相互分离；Skills 不授予 Tool，也不扩大文件访问或 task state。

同步 `task` 使用双向显式 allowlist，而不是传递整个 Deep Agent state：

- 父级到 worker：恰好一条任务 `HumanMessage`，以及存在时冻结的 `memory_context`；
- worker 到父级：最后一条非空 `AIMessage`，以及存在时的 `structured_response`。

Todo、`async_tasks`、Provider search profile、Tool Profile、Skill metadata、文件读取 transcript 和未知未来字段都不会
跨越该边界。worker 内部 transcript 也不回灌父级。middleware 自有 channel 使用 `PrivateStateAttr`，包括
`ToolProfileMiddleware.active_tool_profile_ids` 和递归收尾的 `remaining_steps`；它们不是 task 或公开 context 合同。
若 worker 既没有非空 `AIMessage`，也没有非空 structured response，输出投影会生成一条有界的明确失败报告，
不会用空 `AIMessage` 伪装成功；已有非空文本或 structured response 的投影语义不变。

异步 delegation 的父会话只持久化有界 task handle。创建任务时冻结的 repository snapshot SHA 随 handle、child
thread/run metadata 传递，后续 update 继续使用同一 SHA。异步 worker 的业务输入仍只有模型生成的 description；
父 conversation、Todo、Skill/Profile state 与完整 Tool transcript 不自动复制。

## Memory、预算与历史

`before_agent` 冻结的 `memory_context` 不进入 system prompt。位于 summarization 内层的 model-call middleware 在最新真实
`HumanMessage` 前临时插入一条引用格式的 Memory `HumanMessage`，明确它可能过时且不是本轮指令。这条消息不写入
state、checkpoint messages 或摘要，不能用于确认身份、权限、当前事实和操作参数。

dynamic prompt 只加入 `Asia/Shanghai` 的自然日和可信配置地区，不加入时分秒；本轮明确给出的任务地点优先，且
不会反向修改配置。Provider 联网来源只属于产生它的 `AIMessage.response_metadata`，不会变成下一轮上下文消息。
Tool observation 使用标准 `ToolMessage(content, artifact)`；runtime-owned 字段不进入模型可见 schema。

统一 Agent 与只读 worker 都使用官方 summarization、同一 model superstep 内每 Tool 最多并行 12 次，并在
`recursion_limit` 只剩 8 个 superstep 时关闭 Tool 生成自然综合。所有非只读 Tool 的审批由 Runtime/Tool authority
统一定义，不通过上下文模式切换。

summarization 的绝对 token trigger/keep 分别由
`ProviderConfig.context_input_token_limit * context_compaction_trigger_ratio/target_ratio` 计算，不写死模型窗口。
composition 启动时先创建配置的离线 token counter，并把同一个 `count_messages` 同时传给 main 与 worker；worker
仍使用关闭 Provider-native search 的只读模型视图生成摘要。real DeepSeek V4 或 native LLM compactor 缺少本地
tokenizer 时启动直接失败，不回退近似计数或发起网络调用。

旧 `ContextService`、prompt-json compiler、动态 catalog/exposure 与 renderer 已删除。仍保留的 context 代码只服务
明确的离线报告、媒体压缩或中立 token/model DTO；生产 Agent Server/native graph 不导入平行 context runtime。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_context_lifecycle.py \
  tests/tdd/unified-assistant-agent
```
