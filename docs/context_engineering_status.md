# LangChain-native Context Engineering

最后更新：2026-08-14

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

生产上下文以 LangChain 标准 `messages` channel 为事实源。fast agent 的 `dynamic_prompt` 每次模型调用从
state 读取父图冻结的 `memory_context`，并从 `Runtime.context` 读取受信 entry profile。Memory 使用带
`memory_context_untrusted_v1` 标记的 XML 数据边界，明确为 untrusted/frozen；身份、权限和 Tool 约束不从
Memory 或用户文本生成。

模型调用上限、Tool 调用上限、只读 Tool retry、长对话 summarization 与 planning 模式非 read Tool HITL
全部使用官方 middleware；fast 模式自动放行。summarization 采用输入窗口 70% 触发、保留 40% 的 token 阈值；它更新标准 message history，
不维护项目自建 conversation/summary state。

planning worker 只获得自己的 objective、父图 Memory 快照、调度器按 `depends_on` 派生的直接上游
`dependency_results` 和同一个 fast agent。依赖结果放入明确的只读数据边界，不能覆盖当前任务、身份、权限或
Tool 约束。worker transcript 不并入父图对话；父图只接收结构化 `WorkerResult`。finalize 使用同一个模型根据
原始请求和按 plan 排序的 worker results 综合标准 `AIMessage`，不把中间结果机械拼接成最终答案。

Tool observation 由标准 `ToolMessage` 表达，结构化 artifact 保留在其 artifact 字段。模型可见 Tool schema
由 LangChain 生成；runtime-owned 身份字段不会进入 schema。Provider 的最终 token 准入由模型窗口配置和官方
middleware处理，局部媒体/文件 adapter 仍必须先执行自己的字节、路径与敏感信息限制。

`src/assistant_agent/context/` 的旧 compiler、catalog 与 report 仍服务尚未迁移的 CLI、自动化和 eval 入口；
生产 Agent Server/native graph 不导入它们。后续外围迁移应复用标准 messages/middleware，不把旧完整
ContextService 搬回父图。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_context_lifecycle.py \
  tests/tdd/native-agent-parent-graph/test_fast_agent.py
```
