# 一、基础概念篇 - Tool Calling、API 与 RAG 的区别

---

## 🔴 原题

在 LLM Agent 中，什么是 tool/function calling？它和普通 API 调用、RAG 检索分别有什么区别？

请从“谁决定调用、输入输出契约、执行边界、失败处理”这几个角度回答。

---

## 我的回答

工具调用是指LLM输出希望使用的工具，通常是结构化字段，代码解析并执行该工具，向LLM返回结果。工具调用：LLM决定调用、输入输出是结构化字段

---

## 面试官点评

答对的部分：
- 你抓住了 tool/function calling 的核心：LLM 输出结构化工具调用意图，代码侧解析、执行，再把结果返回给 LLM。
- “LLM 决定调用”和“输入输出是结构化字段”这两点是必须说出来的。

不足的地方：
- 没有明确强调：LLM 只是“提出调用意图”，真正执行必须由宿主程序、agent runtime 或 tool executor 完成。
- 没有和普通 API 调用、RAG 检索做横向对比，面试官问这个题通常就是想看你能不能区分 agentic tool use、传统工程调用和知识检索链路。
- 没有覆盖失败处理。生产级 tool calling 的关键不是“能调工具”，而是未知工具、参数错误、工具失败、重试、降级和 observation 回传怎么处理。

---

## 标准答案

Tool/function calling 是一种让 LLM 以结构化方式请求外部能力的机制。模型不会直接执行代码或访问外部系统，它只输出类似“工具名 + JSON 参数”的调用意图；宿主程序负责校验参数、执行工具/API/service，再把结构化结果作为 observation 返回给模型，模型基于结果继续推理或给出最终回答。

可以从四个角度区分：

| 维度 | Tool/function calling | 普通 API 调用 | RAG 检索 |
| --- | --- | --- | --- |
| 谁决定调用 | 通常由 LLM 根据用户意图和工具描述决定是否调用、调用哪个工具 | 由业务代码按固定流程决定 | 常由应用 pipeline 决定，也可以包装成一个工具交给 LLM 决定 |
| 输入输出契约 | 工具名、JSON schema 参数、结构化 ToolResult/observation | 函数签名、HTTP schema、SDK 类型或业务 DTO | 查询、top-k、过滤条件；输出通常是文档片段、metadata、score |
| 执行边界 | LLM 只提出意图；runtime 校验后执行，工具侧调用 service/provider | 应用代码直接调用外部 API 或内部服务 | retriever/vector store/search service 执行检索，结果进入上下文 |
| 失败处理 | runtime 需要处理未知工具、缺参、schema 错误、权限、预算、超时、重试和失败 observation | 调用方直接处理异常、重试或返回错误 | 检索为空、召回差、超时或上下文过长时，通常降级为无检索回答或提示信息不足 |

一句话概括：

- 普通 API 调用是“代码决定并执行”。
- RAG 是“为回答补充外部知识上下文”。
- Tool calling 是“LLM 决定需要外部能力，但由受控 runtime 负责校验和执行”。

工程上还要补一句：tool calling 不能只靠 prompt 约束。可靠系统必须有 schema 校验、工具白名单、执行前 validator、统一 executor、结构化错误和可观测记录。否则模型一旦传错参数、调用不存在的工具或重复失败调用，系统就会变成不可控的外部能力入口。

结合本项目，可以这样落地说明：

```text
LLM / provider-native tool call
  -> AssistantDecision(type="tool_call")
  -> ActionValidator.validate()
  -> ToolExecutor.run_tool()
  -> ToolRegistry.run()
  -> tool.run(input, ToolContext)
  -> ToolResult
  -> ToolObservation
  -> 下一轮 LLM
```

本项目里 `ToolSpec` 是 LLM 和 provider 看到的工具契约，包含 `name`、`description`、`input_schema`、`required_inputs`、`when_to_use`、`when_not_to_use` 和 `runtime_constraints`。`ToolRegistry.list_specs()` 从已注册工具生成这些契约，`tool_spec_adapters.py` 再把它转换成 OpenAI-compatible tools 或 MCP tool schema。

`AssistantDecision` 是内部统一决策协议。prompt-json 模式下，LLM 输出的 JSON 会解析成 `AssistantDecision`；provider-native 模式下，原生 tool call 会先归一化成同一个 `AssistantDecision(type="tool_call")`，再进入相同的校验和执行链路。

`ActionValidator` 是执行前拦截层，负责拒绝未知工具、缺少工具名、非 JSON object 输入、关键语义参数缺失，以及 memory、render、agent delegation 等特殊安全条件。`ToolExecutor` 是唯一执行边界，负责绑定运行时身份、检查 provider budget、记录状态和事件、执行 retry/recovery、捕获异常并返回结构化 `ToolResult`。外部入口不能绕过它直接调用 `registry.run()`。

---

## 面试金句

> Tool calling 不是让 LLM 直接调用外部系统，而是让 LLM 提出结构化调用意图；真正的执行权必须留在受控 runtime 里。

> 普通 API 调用是代码驱动，RAG 是知识补充，tool calling 是模型驱动的受控能力调用。

---

## 本项目代码位置

- `src/assistant_agent/schemas/assistant_decision.py`: `AssistantDecision` 与 provider-native tool call 归一化。
- `src/assistant_agent/schemas/tools.py`: `ToolSpec`、`ToolResult`、`ToolCallRecord`。
- `src/assistant_agent/schemas/tool_spec_adapters.py`: `ToolSpec` 到 OpenAI-compatible tools 和 MCP tool schema 的转换。
- `src/assistant_agent/tools/registry.py`: 工具注册、查找、执行和 `list_specs()`。
- `src/assistant_agent/agent/action_validator.py`: 工具执行前校验。
- `src/assistant_agent/agent/tool_executor.py`: 工具执行、身份绑定、预算、retry/recovery、event/history/trace。
- `src/assistant_agent/agent/assistant_loop_nodes.py`: assistant loop 中的决策、校验、执行和 observation 回传。
- `tests/test_native_tool_call_handoff.py`: provider-native tool call 进入 validator/executor 并回传 observation。
- `tests/unit/test_tool_spec_adapters.py`: ToolSpec schema 转 OpenAI/MCP tool schema。
- `tests/test_tool_executor.py`: ToolExecutor 成功执行并更新状态。
- `tests/test_architecture_boundaries.py`: API、WebSocket、MCP 不绕过 ToolExecutor 直接调用 registry。
