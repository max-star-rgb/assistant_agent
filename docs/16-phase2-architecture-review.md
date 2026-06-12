# 16 Phase 2 架构审计与技术债

## 1. 当前真实实现的能力

当前代码已经实现了一个可本地测试的多模态 Agent MVP：

- 请求与响应 Schema：`src/multimodal_agent/schemas/requests.py` 定义 `UserRequest`、`AgentResponse`。
- 领域 Schema：感知、商品、工具结果、记忆、生成结果、计划结构分别位于 `src/multimodal_agent/schemas/` 下的具体模块。
- Agent 状态：`src/multimodal_agent/agent/state.py` 提供 `AgentState`，包含 intent、plan、tool calls、tool results、errors、response 等运行状态。
- 意图识别与路由：`src/multimodal_agent/agent/intent.py` 和 `src/multimodal_agent/agent/router.py` 以规则方式完成 intent detection、tool selection、task plan 生成。
- 同步 Agent workflow：`src/multimodal_agent/agent/workflow.py` 串联 intent、router、tool registry、工具执行、响应合成、运行历史和工具调用历史。
- Tool Registry：`src/multimodal_agent/tools/registry.py` 提供注册、查询、执行工具的统一入口，`src/multimodal_agent/tools/__init__.py` 只暴露 registry 公共入口。
- 工具层：视觉理解、商品搜索、比价、图片生成、3D 渲染、记忆读写工具已实现结构化输入输出和错误结果。
- Adapter 层：视觉、商品、图片生成、3D 渲染均有 Protocol 和 Mock 实现，位置在 `src/multimodal_agent/services/*_adapter.py`。
- 记忆：`src/multimodal_agent/memory/store.py` 提供内存存储，`src/multimodal_agent/memory/jsonl_store.py` 提供 JSONL 持久化存储。
- API：`src/multimodal_agent/api/app.py`、`routes_agent.py` 提供 FastAPI health 和 agent run 接口。
- WebSocket：`src/multimodal_agent/api/websocket.py` 提供 mock progress event 流。
- 观测：`src/multimodal_agent/services/run_history.py` 和 `tool_history.py` 提供本地运行历史与工具调用历史。
- 多步规划：`src/multimodal_agent/agent/planner.py` 提供规则式 planner，支持至少 3 步任务拆解。
- 评估：`tests/evals/eval_cases.json` 和 `scripts/run_evals.py` 提供固定评估集与通过率统计。

## 2. 仍然是 Mock 的能力

以下能力仍是 mock/local deterministic 实现，没有接入真实外部 Provider：

- 视觉理解：`MockVisionUnderstandingAdapter` 返回固定结构化视觉结果。
- 商品搜索和比价：`MockProductSearchAdapter` 返回固定商品候选和本地排序结果。
- 图片生成：`MockImageGenerationAdapter` 返回 `local://generated/poster.png`。
- 3D 渲染：`MockRenderAdapter` 返回 `local://render/preview.png` 和 `local://render/model.glb`。
- WebSocket 事件：`mock_agent_events()` 返回固定进度事件。
- 记忆工具：`MemoryTool` 仍是 mock 风格的结构化保存/检索结果；JSONL 持久化已存在，但没有统一接入主 workflow 的长期存储策略。
- Provider 配置：`src/multimodal_agent/config.py` 只读取环境变量和判断是否有真实 provider 配置，不初始化真实客户端。

## 3. LangGraph 体现在哪些文件

LangGraph 已经真实出现在以下文件：

- `src/multimodal_agent/agent/graph.py`
  - 使用 `StateGraph`、`START`、`END`。
  - 定义线性节点：`detect_intent`、`route_tools`、`execute_tools`、`compose_response`。
  - 通过 `graph.compile()` 生成可执行 graph。
- `src/multimodal_agent/agent/conditional_graph.py`
  - 使用 `StateGraph`、`START`、`END`。
  - 使用 `add_conditional_edges()` 按 intent 分流到 vision/search/compare/image/render/memory/chat/multi_tool 节点。
  - 通过 `graph.compile()` 生成可执行 conditional graph。
- `tests/test_langgraph_workflow.py`
  - 覆盖最小线性 graph 的 compile 和执行。
- `tests/test_langgraph_routing.py`
  - 覆盖 conditional graph 的 compile、route function、执行路径。

当前技术债：`AgentWorkflow.run()` 默认仍走 `workflow.py` 的自研同步流程，LangGraph 是可调用的旁路实现，还没有成为 API 默认编排入口。

## 4. Tool 是否仍通过 Adapter 调用

主要外部能力类 Tool 仍通过 Adapter 调用：

- `VisionUnderstandingTool` 通过 `VisionUnderstandingAdapter.understand()`。
- `ProductSearchTool` 通过 `ProductSearchAdapter.search()`。
- `PriceCompareTool` 通过 `ProductSearchAdapter.compare()`。
- `ImageGenerationTool` 通过 `ImageGenerationAdapter.generate()`。
- `Render3DTool` 通过 `RenderAdapter.create_render()`。

Tool Registry 负责注册和调用 Tool，本身不直接处理 provider 细节。记忆类工具当前没有独立 adapter 层，属于本地 mock 工具能力。

## 5. 是否有直接 Provider 调用泄漏到 Tool

当前没有发现 Tool 直接调用真实 Provider SDK、HTTP 客户端或外部服务。

扫描范围包含 `src/` 和 `tests/` 中的 provider、adapter、mock、HTTP 相关关键字。代码中没有 `openai`、`anthropic`、真实 HTTP `post/get` provider 调用泄漏到 `src/multimodal_agent/tools/`。API 测试中的 `client.get()`、`client.post()` 是 FastAPI TestClient 调用，不属于外部 Provider 泄漏。

Provider 相关环境变量集中在 `src/multimodal_agent/config.py`，目前只用于配置读取和测试。

## 6. `__init__.py` 聚合导出情况

当前 `__init__.py` 使用情况基本符合“业务对象从具体模块导入”的约定：

- `src/multimodal_agent/__init__.py` 只导出 `__version__`，没有聚合业务对象。
- `src/multimodal_agent/tools/__init__.py` 只导出 `ToolRegistry` 和 `create_default_registry`，符合工具包公共注册入口定位。
- `src/multimodal_agent/schemas/__init__.py` 只保留说明性 docstring，没有聚合导出 Schema。
- `src/multimodal_agent/api/__init__.py`、`agent/__init__.py`、`memory/__init__.py`、`services/__init__.py` 当前没有业务聚合导出。

测试和源码中的业务导入基本都来自具体模块，例如 `multimodal_agent.schemas.requests`、`multimodal_agent.tools.registry`、`multimodal_agent.services.product_adapter`。

## 7. 测试覆盖缺口

当前测试覆盖了 Schema、AgentState、intent/router、tool registry、mock tools、adapter、memory、API、WebSocket、LangGraph、planner、eval runner 和 e2e demo。主要缺口如下：

- 默认 API 入口没有覆盖“使用 LangGraph 作为主 workflow”的路径。
- `conditional_graph.py` 覆盖了主要路由和执行，但还缺少每个 intent 分支的更细粒度失败路径测试。
- JSONL memory store 已单测持久化读写，但主 workflow 没有接入持久化 memory store，因此缺少跨会话/重启语义测试。
- Provider integration 目前只有配置读取测试，没有真实 provider adapter 的契约测试或 skip-by-env 集成测试。
- 评估集是固定规则样例，尚未覆盖更多失败场景、中文口语变体和多模态输入组合。
- API/WebSocket integration tests 默认跳过，需要 `RUN_INTEGRATION_TESTS=1` 才执行。

## 8. Phase 3 建议任务

建议 Phase 3 按以下顺序推进：

1. 将 LangGraph 设为主编排入口，保留 `AgentWorkflow.run()` 兼容包装，并让 API 默认走 graph。
2. 抽出 graph node 与 workflow 私有方法之间的边界，减少 LangGraph 节点调用 `_build_tool_input()`、`_run_tool()`、`_compose_response()` 这类私有方法。
3. 统一 memory store 接口，把 JSONL store 接入可配置的本地持久化路径，并明确短期记忆与长期记忆职责。
4. 为真实 provider adapter 增加契约测试和 env-gated integration tests，保持默认不调用外部服务。
5. 扩展评估集，增加失败样例、模糊指代、多步依赖和多模态组合样例。
6. 为 LangGraph 引入显式多步循环执行节点，让 planner 结果由 graph 驱动，而不是继续依赖同步 workflow 中的 for-loop。
7. 整理生成物和缓存文件策略，避免 `__pycache__`、egg-info 等构建产物进入审计视野或版本控制。
