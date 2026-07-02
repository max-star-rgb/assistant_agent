# Tool Calling Architecture

本文件是 tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、provider-native tool calls 和 MCP `tool_run` 的当前权威入口。涉及记忆工具或 agent delegation 的服务内部设计时，还要分别阅读 `docs/memory-service-architecture.md` 和 `docs/agent-communication-routing.md`；本文件只定义工具调用边界和运行链路。

## 新对话快速交接

- 默认运行时是 `AgentGraphRuntime` + LangGraph assistant loop；`agent_graph_mode=assistant_loop` 是当前主路径，conditional/legacy graph 只保留兼容。
- 工具契约由 `ToolSpec` 表达，来源是 `ToolRegistry.list_specs()`；prompt-json 模式把筛选后的 ToolSpec 渲染进提示词，provider-native 模式把完整 ToolSpec 转成 OpenAI-compatible tools schema。
- LLM 或 mock 只能提出 `AssistantDecision`。provider-native tool call 会先归一化成 `AssistantDecision(type="tool_call")`，再走同一套校验和执行。
- 工具执行必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。API、WebSocket、MCP 或新增入口都不能直接 `registry.run(...)`。
- `ToolExecutor` 是运行时治理边界：绑定 user/session 身份、检查 provider budget、记录 state/tool history/event/trace、执行 retry/recovery，再调用 registry。
- 工具实现应保持薄适配层：Pydantic input/output schema、调用 adapter/service、包装 `ToolResult` 和 `CapabilityOutputContract`。真实外部能力必须在 provider/service adapter 层受 runtime profile 控制。
- 工具 observation 是下一轮 LLM 的数据，不是系统指令；写入 prompt 前会被摘要、脱敏、压缩。不要把 provider raw response、密钥、base64 大 payload 暴露给 assistant context。
- 当前 provider-native 模式每个 assistant iteration 只执行第一个 native tool call；多步任务通过 ReAct 多轮或 plan mode 完成。

## 当前主调用链

```text
UserRequest
  -> AgentGraphRuntime.run_state
  -> build_assistant_loop_graph
  -> load_memory
  -> assistant_node
       -> ToolRegistry.list_specs()
       -> build_assistant_context_pack()
       -> prompt-json ChatRequest OR provider-native ChatRequest(tools=[...])
       -> ChatAdapter.chat()
       -> AssistantDecision
  -> route_after_assistant
  -> execute_requested_tool_node
       -> plan step check, if plan mode is active
       -> ActionValidator.validate()
       -> ToolExecutor.run_tool()
            -> bind runtime identity
            -> ProviderCallBudget.check_before_call()
            -> AgentState.add_tool_call()
            -> tool_started event / ToolHistoryStore.record_start()
            -> ToolRegistry.run()
            -> tool.run(input, ToolContext)
            -> provider budget record, retry/recovery, events/history/trace
       -> ToolObservation
  -> assistant_node again, or compose_response
  -> save_memory
```

相关源码：

- `src/assistant_agent/agent/runtime.py`: 组装 registry、memory manager、chat adapter、tool executor、trace store 和 graph。
- `src/assistant_agent/agent/assistant_loop_nodes.py`: ReAct 决策、native tool handoff、validator 调用、observation 回传和 loop guard。
- `src/assistant_agent/agent/action_validator.py`: 工具执行前的本地校验边界。
- `src/assistant_agent/agent/tool_executor.py`: 工具执行、预算、retry/recovery、event/history/trace 的统一边界。
- `src/assistant_agent/tools/registry.py`: 工具注册、ToolSpec 生成和默认工具集合。
- `src/assistant_agent/tools/base.py`: `ToolContext`、`BaseTool`、`MockTool` 基础契约。
- `src/assistant_agent/schemas/tools.py`: `ToolSpec`、`ToolResult`、`ToolCallRecord`。
- `src/assistant_agent/schemas/assistant_decision.py`: 内部 assistant decision 和 native tool call 归一化。
- `src/assistant_agent/schemas/tool_spec_adapters.py`: ToolSpec 到 OpenAI/MCP 工具 schema 的转换。
- `src/assistant_agent/schemas/tool_observation.py`: assistant-facing observation 摘要和脱敏。

## 两种 assistant tool calling 模式

`ProviderConfig.assistant_tool_call_mode` 支持：

- `auto`: 默认值。非 mock chat adapter 使用 provider-native tools；mock adapter 仍走离线 rule plan。
- `native_tools`: 强制真实/脚本 chat adapter 走 provider-native tools。
- `prompt_json`: 不向 provider 传 native tools，只要求模型输出 `AssistantDecision` JSON。

prompt-json 模式：

- `assistant_node` 通过 `render_assistant_prompt()` 输出完整决策契约。
- `build_assistant_context_pack()` 会用 `select_prompt_tool_specs()` 选择较小的 prompt-facing ToolSpec 子集，降低上下文成本。
- LLM 输出由 `AssistantDecision.from_llm_output()` 解析；JSON 形态损坏时只做一次 repair，不直接执行原始损坏输出。

provider-native 模式：

- `assistant_node` 通过 `tool_specs_to_openai_tools(context.tool_specs)` 把完整 ToolSpec 列表转换成 provider tools。
- native user message 只包含请求、对话、记忆、plan mode 状态，不重复渲染 ToolSpec。
- `ChatResult.tool_calls[0]` 通过 `NativeToolCall.to_assistant_decision()` 转成内部 `tool_call` 决策。
- observation 会作为后续 native messages 中的 `tool` role 内容回传。
- provider 返回 refusal 或普通文本时，直接转成 terminal final answer，不进入工具执行。

## ToolSpec 契约

`ToolSpec` 是唯一工具契约视图：

```text
name
description
input_schema
required_inputs
when_to_use
when_not_to_use
runtime_constraints
```

ToolSpec 由 `ToolRegistry.list_specs()` 从工具类的 Pydantic `input_schema` 生成，并补充 `_ACTION_USAGE` 中的使用条件和运行约束。默认约束包含 `Use only through ToolExecutor.`。

注意事项：

- memory dedicated tools 会隐藏 `user_id`、`session_id` 这类运行时身份字段；模型不应决定记忆归属。
- `tool_spec_to_json_schema()` 会输出 object schema，并设置 `additionalProperties=False`。
- prompt-json 模式可能只展示一部分 ToolSpec；provider-native tools 发送完整 ToolSpec。
- MCP 工具 schema 通过 `tool_spec_to_mcp_tool()` 支持，但当前 `OfflineMCPServer.list_tools()` 暴露的是 MCP wrapper 工具；registry ToolSpec 通过 `tool_list` 返回。

## 当前默认工具

`create_default_registry()` 默认注册：

- `vision_understanding`
- `video_understanding`
- `product_search`
- `price_compare`
- `image_generation`
- `render_3d`
- `memory`
- `memory_retrieval`
- `memory_save`

`delegate_to_agent` 是 opt-in 工具，仅在 `enable_agent_delegation=True` 且传入 `AgentCommunicationService` 时注册。它的通信路由和 A2A 边界以 `docs/agent-communication-routing.md` 为准。

## ActionValidator 边界

`ActionValidator.validate()` 在执行前拒绝不安全或不可执行的 action。当前校验包括：

- `decision.type != "tool_call"` 时不执行工具。
- 必须有 `tool_name`，`tool_input` 必须是 JSON object。
- `tool_name` 必须存在于当前 registry。
- `vision_understanding` 必须有 `image_ids`，`video_understanding` 必须有 `video_ref` 或 `video_ids`。
- `image_generation` 必须有 prompt 或 product information。
- `product_search` 必须有 query 或 visual summary。
- `price_compare` 必须有 query 或 items。
- `memory_retrieval` 必须有 query。
- legacy `memory` 只允许 `action=retrieve/save`，并复用 memory save 校验。
- `memory_save` 必须有 query、`content.text` 或 `content.summary`，且 assistant-loop 调用必须包含 `source_intent`、`source_reason`、`future_use`、`evidence`。
- `source_intent=user_confirmed` 保留给确认服务，assistant-loop 不得使用。
- `render_3d` 必须有明确 3D/render/modeling/scene-preview 意图。
- `delegate_to_agent` 必须有 `target_agent_id` 和 text/image/video/audio payload。
- 最后用目标工具的 Pydantic `input_schema` 做结构校验。

被拒绝的 action 不会进入 `ToolExecutor`，不会产生 `ToolCallRecord`；assistant loop 会生成 `ToolObservation(status="rejected")`，记录 `action_rejected` trace，并由 loop guard 判断是否终止。

## ToolExecutor 边界

`ToolExecutor.run_tool()` 是唯一工具执行入口。它负责：

- 对 memory 工具用 `AgentState.user_id/session_id` 覆盖模型参数。
- 检查 run-scoped cancel token；取消时跳过工具执行或停止 retry，并把 run 交回 runtime 标记为 `cancelled`。
- 创建 `ToolCallRecord` 并更新 `AgentState.status`。
- 在真正执行前调用 `ProviderCallBudget.check_before_call()`。
- 发出 `tool_started`、`tool_finished`、`tool_failed` 事件。
- 写入 `ToolHistoryStore` 的 started/succeeded/failed 记录。
- 通过 `ProviderExecutionPolicy.retry` 对 retryable provider 错误重试。
- 把 registry/tool 异常转为失败 `ToolResult`，不让异常穿透给 Agent。
- `AgentRunCancelled` 是例外：它是运行时控制信号，会穿透 graph node 并由 `AgentGraphRuntime` 收口。
- 记录 provider budget call record。
- 用 `RecoveryPolicy` 决定失败后 stop、partial continue 或 optional step skip。
- 写入 trace event，失败时包含 sanitized error 和 input/output summary。

只有 `ToolExecutor._run_once()` 可以直接调用 `registry.run(...)`。新增入口、API、MCP、graph node 或 service 不应直接调用 registry。

`ToolContext` 携带 `run_id/user_id/session_id`、运行时 metadata，以及可选的 `cancel_token`。工具若有自然轮询点，可以调用 `context.is_cancelled()` 提前返回；不要求现有同步工具在本阶段改成 async。

## ToolResult 和 observation

工具必须返回 `ToolResult`：

```text
tool_name
success
data
error
output_ref
latency_ms
contract
```

实践规则：

- `data` 必须是结构化 JSON-compatible dict；不要只塞散乱字符串。
- 推荐提供 `CapabilityOutputContract`，并在 `data["contract"]` 中保留 JSON 形式，便于 API/WebSocket/response composer 统一消费。
- `error` 应是可解释、已脱敏的错误信息。
- `output_ref` 用于引用生成物、搜索结果、记忆项、agent task 等 artifact。
- provider/model/cost 等可放在 `data` 或 contract metadata，让 provider budget 和 trace 摘要可记录。

`ToolObservation` 是 assistant-facing 摘要，不等同于原始 `ToolResult`：

- 成功 observation 包含 summary、output_ref、structured_output、next_step_hint。
- 失败或 rejected observation 包含 sanitized error_code/error_message 和 recovery hint。
- 商品搜索/比价会保留 title、price、currency、URL、url_status 等后续回答和 price_compare 必需字段。
- context builder 会压缩大 observation、截断命令输出、移除 raw provider payload、base64、secret-like 内容。

## Loop guard 和计划模式

assistant loop 有本地保护，不依赖模型自律：

- `MAX_TOOL_ITERATIONS` 默认 5；接近上限时会要求模型 final answer，不再继续工具调用。
- unknown tool、invalid input、missing required input、render intent 缺失等会触发 rejection guard。
- 同一工具失败达到阈值会停止重复调用。
- `image_generation` 和 `render_3d` 是 terminal tools；成功后再次请求同一工具会被阻止并转为 final answer。
- 明确比价请求中，`product_search` 成功后会强制或修复下一步 `price_compare`，防止只搜索不比价。

plan mode 是同一 ReAct loop 内的状态，不是独立 planner/controller：

- `enter_plan_mode` 的 `TaskPlan` 先经 `PlanValidator` 校验 step 数量、唯一 step_id、依赖、未知工具和环。
- active plan mode 下，tool call 必须匹配计划步骤和依赖。
- 工具失败后 plan mode 可进入 `replanning`。

## Memory tool 特殊规则

Memory 工具选择采用 LLM-first：

- 本地关键词和向量信号只记录 audit，不覆盖 LLM 是否调用 `memory_retrieval` / `memory_save`。
- `memory_save` 必须声明 `source_intent`、`source_reason`、`future_use`、`evidence`。
- `source_intent=user_explicit` 只用于用户明确要求保存/以后使用。
- `source_intent=assistant_candidate` 只记录候选，不直接写长期记忆。
- `source_intent=user_confirmed` 只能由确认服务使用，assistant-loop 调用会被 validator 拒绝。
- `ToolExecutor` 和 `MemoryTool` 都会用运行时 `ToolContext` 绑定身份，模型传入的 user/session 不作为记忆归属。

记忆服务内部检索、写入策略、确认、TTL、审计和 store 选择以 `docs/memory-service-architecture.md` 为准。

## MCP 和外部入口

`OfflineMCPServer` 提供四个 MCP wrapper 工具：

- `agent_run`: 走 `AgentGraphRuntime`。
- `tool_list`: 返回 MCP wrapper 列表和 registry ToolSpec。
- `tool_run`: 构造 `AssistantDecision(type="tool_call")`，先过 `ActionValidator`，再调用 `ToolExecutor`。
- `demo_flow_run`: 走离线 demo flow。

MCP server 不直接依赖 provider SDK，不直接访问 OpenAI/DashScope/httpx/requests。错误 envelope 会脱敏。新增外部入口必须遵守同样边界：先归一成内部 request/decision，再走 validator/executor。

## 新增或修改工具清单

新增工具时按这个顺序做：

1. 定义 Pydantic input/output schema。通用 schema 放 `schemas/`，工具私有 schema 可放工具模块。
2. 实现 `MockTool` 或满足 `BaseTool` 协议：`name`、`description`、`input_schema`、`output_schema`、`run/_run`。
3. 真实能力先建 service/provider adapter interface 和 mock/local implementation；工具只调用 adapter/service。
4. 返回结构化 `ToolResult`，失败也要返回可解释错误和可选 contract，不抛未处理异常。
5. 在 `ToolRegistry.create_default_registry()` 注册。默认注册只放 mock/local/offline 安全工具；高风险或跨 agent 工具用显式开关。
6. 在 `_ACTION_USAGE` 增加 `when_to_use`、`when_not_to_use`、`runtime_constraints`。
7. 如有语义必需参数或安全条件，在 `ActionValidator` 增加执行前校验。
8. 如旧 mock/rule plan 需要支持，在 `tool_input_builder.py` 增加 action 到 tool input 的兼容构造。
9. 如 observation 后续会驱动另一个工具，更新 `tool_observation.py` 的 summary/next_step_hint/保留字段。
10. 如涉及 provider-native 或 MCP schema，补充 `tool_spec_adapters` 相关测试。
11. 如涉及 memory 或 agent delegation，先按对应权威文档确认服务边界。

最小测试覆盖：

- registry spec 暴露和 schema 转换。
- ActionValidator 接受合法输入、拒绝缺参/未知工具/危险输入。
- ToolExecutor 成功、失败、预算阻断、retry/recovery 和 cooperative cancellation。
- assistant_loop prompt-json 路径和 provider-native 路径。
- observation 和 trace/event 中不出现 raw provider payload、API key、Authorization、Bearer token。
- 若工具可默认注册，离线 pytest 和 demo flow 不能依赖真实 provider。

## 当前验证入口

优先跑这些离线测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_tool_executor.py \
  tests/test_provider_budget_in_tool_executor.py \
  tests/test_retry_policy.py \
  tests/test_phase8a1_react_action_quality.py \
  tests/test_native_tool_call_handoff.py \
  tests/test_assistant_context_renderer.py \
  tests/unit/test_tool_spec_adapters.py \
  tests/test_mcp_server_skeleton.py \
  tests/test_architecture_boundaries.py
```

更大范围验收：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

## 不要做

- 不要让 API、WebSocket、MCP、demo 或 eval 直接调用 provider SDK 或 `registry.run(...)`。
- 不要只靠 prompt 约束防止危险工具调用；必须在 `ActionValidator` 或 service policy 层落地。
- 不要把 model-provided user_id/session_id 当作身份来源。
- 不要在工具里写入 API key、raw provider response、真实用户数据 dump 或大媒体 payload。
- 不要把 mock/offline 行为伪装成真实 LLM/provider 能力。
- 不要把 `docs/development/**` 当作当前 tool calling 设计权威。
