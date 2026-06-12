# 25 Phase 3 架构审计与发布检查

## 1. 默认 Runtime 入口

Phase 3 后，默认 Agent Runtime 是 `AgentGraphRuntime`：

- HTTP API：`src/multimodal_agent/api/routes_agent.py` 的 `/agent/run` 通过 `get_agent_runtime().run_state()` 调用 graph runtime。
- WebSocket：`src/multimodal_agent/api/websocket.py` 通过 graph runtime 生成真实工具事件和最终响应/错误事件。
- 兼容入口：`src/multimodal_agent/agent/workflow.py` 的 `AgentWorkflow.run()` 默认委派到 `AgentGraphRuntime`，`run_legacy()` 仅保留兼容同步路径。

当前默认路径不再依赖旧同步 workflow 的 for-loop。

## 2. LangGraph 文件和节点

LangGraph 主要位于：

- `src/multimodal_agent/agent/conditional_graph.py`
  - 默认 runtime 使用的条件图。
  - 包含 `load_memory`、`detect_intent`、单步工具节点、`plan_steps`、`select_next_step`、`execute_step`、`compose_response`、`save_memory`。
  - 对 `execute_step` 使用 `add_conditional_edges()` 实现多步循环。
- `src/multimodal_agent/agent/graph.py`
  - 保留线性 graph 测试和兼容入口。
- `src/multimodal_agent/agent/graph_nodes.py`
  - 存放可复用 node 函数和 loop 路由函数。
- `src/multimodal_agent/agent/runtime.py`
  - 负责注入 registry、memory store、config、intent detector、router、tool executor，并 invoke compiled graph。

测试覆盖：

- `tests/test_langgraph_workflow.py`
- `tests/test_langgraph_routing.py`
- `tests/test_langgraph_multistep_loop.py`
- `tests/test_agent_runtime.py`

## 3. Node 边界是否干净

Phase 2 中 graph node 仍调用 `AgentWorkflow` 私有方法。Phase 3 已拆分为公共边界组件：

- `src/multimodal_agent/agent/tool_input_builder.py`
- `src/multimodal_agent/agent/tool_executor.py`
- `src/multimodal_agent/agent/response_composer.py`
- `src/multimodal_agent/agent/graph_nodes.py`

`graph.py`、`conditional_graph.py`、`graph_nodes.py` 不再调用 `_build_tool_input()`、`_run_tool()`、`_compose_response()`、`_save_demo_memory()` 等 workflow 私有方法。对应静态检查在 `tests/test_graph_node_boundaries.py`。

## 4. Memory Backend 策略

当前支持两种 memory backend：

- 默认：`MULTIMODAL_AGENT_MEMORY_BACKEND=memory`，使用 `InMemoryStore`，避免污染开发环境。
- 本地持久化：`MULTIMODAL_AGENT_MEMORY_BACKEND=jsonl`，使用 `JsonlMemoryStore`。

路径配置：

```text
MULTIMODAL_AGENT_MEMORY_PATH=.local/memory/memories.jsonl
```

runtime 可直接注入 `memory_store`，也可从 `ProviderConfig` 创建。Graph 会在开始时加载 `memory_context`，完成时保存 task memory。覆盖测试在 `tests/test_memory_runtime_integration.py`。

## 5. Provider Contract Tests

真实 Provider 尚未接入，但已建立 MockAdapter 契约测试：

- `tests/contracts/test_vision_adapter_contract.py`
- `tests/contracts/test_product_adapter_contract.py`
- `tests/contracts/test_image_generation_adapter_contract.py`
- `tests/contracts/test_render_adapter_contract.py`

这些测试默认运行，验证输入输出 schema、错误路径和 Tool 层结构化结果。Tool 层仍只依赖 adapter protocol，不直接感知 provider。

## 6. Integration Tests Skip 策略

Integration tests 默认 skip，由 `tests/integration/conftest.py` 控制：

```text
RUN_INTEGRATION_TESTS=1
```

未设置时 integration tests 不执行真实服务。即使显式运行 provider integration 配置测试，缺少真实 provider 配置也会 skip，不会失败，也不会要求 API Key。

## 7. Eval 指标

Eval cases 已扩展到 30 条，位于 `tests/evals/eval_cases.json`，覆盖：

- intent
- routing
- multistep
- memory
- failure / ambiguous input
- multimodal input combination

`scripts/run_evals.py` 输出：

- `total`
- `passed`
- `failed`
- `pass_rate`
- `intent_accuracy`
- `tool_selection_accuracy`
- `ordered_tool_match`
- `unexpected_tool_rate`
- `failed_case_ids`

当前离线 eval 结果为 30/30 通过。

## 8. 是否仍有 Mock 能力

仍是 Mock/local 能力：

- `MockVisionUnderstandingAdapter`
- `MockProductSearchAdapter`
- `MockImageGenerationAdapter`
- `MockRenderAdapter`
- 记忆工具的本地结构化保存/检索
- WebSocket 事件来自本地 graph runtime，不是异步任务队列或真实外部服务

当前没有接入真实付费 API，没有真实 Provider SDK 调用，也没有密钥写入代码或文档。

## 9. Phase 4 建议

建议 Phase 4 以真实生产边界为主：

1. 增加真实 Provider Adapter 的可选实现，并保持 env-gated integration tests。
2. 把 WebSocket 从同步 runtime 事件升级为长任务事件流，可接本地队列或 Redis/Celery。
3. 引入更明确的 memory 检索策略：关键词检索、类型过滤、摘要压缩、后续再考虑向量检索。
4. 增加失败恢复策略：工具失败后是否跳过、重试、请求用户确认。
5. 增加 graph execution trace，便于调试 LangGraph 节点路径和状态变化。
6. 增加 API 级错误码和响应协议版本，避免前端对内部状态结构耦合。
7. 进一步清理构建产物和缓存文件，确保发布包只包含源码、文档和必要测试资产。
