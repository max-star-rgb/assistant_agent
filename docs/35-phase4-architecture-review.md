# 35 Phase 4 架构审计报告

## 结论

Phase 4 已完成生产化边界增强：默认 Runtime 是 `AgentGraphRuntime`，真实 Provider 仍为可选能力，默认测试和默认运行路径继续使用本地 Mock/Local 实现。HTTP、WebSocket、Trace、Recovery、Memory、TaskQueue 均已形成可测试的稳定边界。

当前系统仍处于本地可验证阶段，不应被描述为完整生产部署。真实外部服务、分布式队列、持久化 Trace、权限系统和生产级可观测性仍属于 Phase 5 之后的工作。

## 1. 真实 Provider 接入状态

真实 Provider 当前只接入视觉理解路径：

- `ProviderConfig` 支持 `MULTIMODAL_AGENT_VISION_PROVIDER=openai|qwen`。
- `create_vision_adapter()` 根据配置选择 `HttpVisionProviderAdapter` 或默认 `MockVisionUnderstandingAdapter`。
- `HttpVisionProviderAdapter` 使用标准库 HTTP 调用，缺少 API Key 时返回 `provider_unconfigured`。
- OpenAI/Qwen 的 base URL、model 和 API Key 从环境变量读取。

当前未接入真实商品搜索、图片生成、3D 渲染或向量数据库 Provider。

## 2. 默认 Mock 与真实 Provider 边界

默认边界是明确的：

- 未设置 provider 环境变量时，视觉理解默认使用 `MockVisionUnderstandingAdapter`。
- 默认 `AgentGraphRuntime()` 通过 `ProviderConfig.from_env()` 加载配置，但默认 `vision_provider` 是 `mock`。
- 测试默认不调用真实外部服务。
- 真实 Provider 只有在环境变量显式配置，并且 integration test gate 打开时才会尝试调用。

这保证了本地开发、CI 和离线 eval 的稳定性。

## 3. Integration Tests Skip 策略

Integration tests 由 `tests/integration/conftest.py` 统一 gate：

- 默认跳过 `tests/integration`。
- 只有 `RUN_INTEGRATION_TESTS=1` 时才执行。
- 真实 Provider 测试还会继续检查 provider 类型、API Key 和必要配置；缺少配置时 skip，而不是失败。

该策略符合 Phase 4 的要求：默认 pytest 不依赖外部服务、不需要密钥。

## 4. WebSocket 事件流

WebSocket 已经不再依赖静态 mock 主流程：

- `/ws/agent/{session_id}` 创建 `ListEventSink`。
- `AgentGraphRuntime(event_sink=sink).run_state()` 执行真实 Graph Runtime。
- Runtime 和 `ToolExecutor` 通过 `AgentEvent` 输出 `task_started`、`graph_node_started`、`tool_started`、`tool_finished`、`tool_failed`、`graph_node_finished`、`final_response`、`task_failed`。
- WebSocket 逐条发送 event sink 中的事件。

错误事件已使用统一 API 错误结构：`code/message/detail/recoverable`。

## 5. TaskQueue 抽象

当前本地任务队列位于 `services/task_queue.py`：

- `TaskQueue` Protocol 定义 `submit()`、`get_status()`、`get_events()`。
- `InlineTaskQueue` 同步执行 Agent 请求并收集事件。
- `InMemoryTaskQueue` 提供本地队列 facade。

当前队列仍是进程内、本地同步实现。Redis、Celery、MQ、任务取消和持久化状态属于后续阶段。

## 6. Memory 检索策略

Memory 检索已从简单存储读取升级为 bounded retrieval：

- `MemoryQuery` 支持 `top_k`、`memory_types`、`session_id`、`since`、`max_context_chars`。
- `MemoryRetrievalStrategy` 支持关键词检索、类型过滤、session/time 过滤、去重、类型优先级和 Top-K。
- `format_memory_context()` 生成有字符上限的上下文文本。
- Graph 的 `load_memory_node` 在请求 metadata 中注入 `memory_context_text`。

当前仍是本地关键词策略，不是向量检索。

## 7. Failure Recovery 策略

失败恢复已集中在 `agent/recovery.py`：

- `RecoveryPolicy` 负责把工具失败映射到稳定错误码和恢复动作。
- 可选步骤失败时允许 `continue_with_partial_result`。
- 关键步骤失败时 `stop_with_error`。
- `ToolExecutor` 将工具失败记录到 `AgentState.errors`，并通过 EventSink/Trace 输出结构化错误。
- 响应合成会说明部分成功或失败原因。
- 错误消息会脱敏 API Key、bearer token、secret 等字段。

当前未实现自动 retry；`max_retries` 是策略字段，实际 retry 仍属于后续增强。

## 8. Graph Trace 能力

Trace 已作为独立于 history 的细粒度调试能力存在：

- `TraceEvent` 记录 `trace_id`、`run_id`、`node_name`、`event_type`、状态摘要、工具名和错误。
- `InMemoryTraceStore` 支持本地测试和节点路径查询。
- `JsonlTraceStore` 支持 JSONL 持久化。
- LangGraph node 通过 `trace_graph_node()` 统一包装，记录 `node_started` / `node_finished`。
- 工具失败记录 `tool_failed` trace。
- Trace 摘要不记录完整请求正文、大文件内容或密钥。

当前默认 runtime 使用内存 trace store。生产持久化和 trace 查询 API 尚未开放。

## 9. API 协议版本

HTTP 和 WebSocket 的错误边界已稳定：

- HTTP `/agent/run` 返回 `protocol_version: "v1"`。
- HTTP response 包含 `run_id`、`trace_id`、`status`、`intent`、`response_text`、`data`、`tool_calls`、`tool_results`、`errors`。
- `ApiError` 统一为 `code/message/detail/recoverable`。
- FastAPI validation error 返回 `INVALID_REQUEST`，不暴露 traceback。
- WebSocket `tool_failed` 和 `task_failed` 使用同一错误结构。

后续如果协议变更，应增加新版本，而不是破坏 `v1` 字段语义。

## 10. Phase 5 建议

建议 Phase 5 聚焦以下方向：

1. 真实 Provider 扩展：为图片生成、商品搜索、渲染增加可选真实 Adapter，并保持默认 mock。
2. 持久化基础设施：Trace、TaskQueue、Memory、Run History 接入可替换持久化后端。
3. 权限与安全：API 鉴权、用户隔离、敏感字段审计、外部工具调用 allowlist。
4. Retry 与降级：把 `RecoveryPolicy.max_retries` 落地到 tool executor 或 graph loop。
5. Trace 查询 API：提供按 `run_id` / `trace_id` 查询节点路径和失败事件的只读接口。
6. 长任务生产化：引入真正异步 worker、取消任务、进度持久化和重放事件。
7. Eval 扩展：加入失败恢复、协议兼容和多轮记忆的回归集。
8. 文档收敛：将 Phase 2/3/4 的临时任务说明沉淀到稳定架构文档中。

## 发布检查

Phase 4 验收命令：

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

