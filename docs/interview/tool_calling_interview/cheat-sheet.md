# 工具调用面试精华速记版

> 面试前 1 小时快速过一遍，只记关键词

---

## 🔧 基础概念篇

### "跳过前置步骤"的幻觉问题

**根因**：LLM 是无状态的推理机，不是有状态的执行器——它不知道自己在哪个步骤

**4 种解决手段（按效果排序）**：
1. **工具执行前状态机校验** - ToolExecutor 层硬卡，100% 防御
2. **ToolSpec 写前置条件** - `when_not_to_use` 字段，90% 有效
3. **System Prompt 加流程约束** - 明确写步骤顺序，80% 有效
4. **少样本示例引导** - 70% 有效

**最有效组合**：状态机校验（兜底） + when_not_to_use（预防）

**本项目设计亮点**：`runtime_constraints` 字段
- 人和机器读同一份约束文档
- LLM 能看到，ToolExecutor 也会校验
- 避免两边理解不一致

**面试金句**：
> 不要相信 LLM 的"自觉"。真正可靠的系统是：LLM 哪怕想做错，系统也不让它做错。

---

### Tool/function calling、API 调用和 RAG 的区别

**定义**：LLM 输出结构化工具调用意图，runtime 校验、执行工具，再把 observation 返回给 LLM。

**核心区别**：

| 类型 | 谁决定 | 主要目的 | 执行边界 |
| --- | --- | --- | --- |
| 普通 API 调用 | 代码 | 调业务能力 | 业务代码直接执行 |
| RAG 检索 | pipeline 或 LLM | 补知识上下文 | retriever/search service |
| Tool calling | LLM 提议，runtime 执行 | 让 Agent 使用外部能力 | validator + executor + registry |

**常见坑**：说成“LLM 调 API”。准确说法是：LLM 只提出调用意图，宿主程序负责校验和执行。

**项目链路**：`AssistantDecision -> ActionValidator -> ToolExecutor -> ToolRegistry -> tool -> ToolResult -> ToolObservation`

**面试金句**：
> 普通 API 调用是代码驱动，RAG 是知识补充，tool calling 是模型驱动的受控能力调用。

---

## 🧩 ToolSpec 设计篇

### 工具定义 / Schema 设计

**核心结构**：`name`、`description`、`input_schema`、`required_inputs`、`when_to_use`、`when_not_to_use`、`runtime_constraints`

**分层边界**：

| 层 | 负责内容 |
| --- | --- |
| JSON schema | 字段、类型、required、枚举、范围、`additionalProperties=False` |
| 描述和 usage 规则 | 什么时候用、什么时候不用、避免工具混淆 |
| ActionValidator | 未知工具、非 object 输入、语义缺参、特殊安全条件 |
| ToolExecutor | 身份绑定、预算、retry/recovery、event/history/trace、异常转结构化失败 |

**常见坑**：把“如何执行”写进 ToolSpec。准确表达是：ToolSpec 写执行约束，具体执行实现留在 runtime/executor/tool class。

**项目链路**：`ToolRegistry.list_specs()` 生成 ToolSpec；prompt-json 渲染进 prompt，provider-native 转 OpenAI tools，MCP 转 MCP tool schema。

**面试金句**：
> JSON schema 只能保证参数长得对，不能保证工具选得对、步骤走得对、执行边界守得住。

---

## ⚙️ 工具执行器篇

### 工具执行器与失败处理

**核心原则**：LLM 只提出调用意图，真实执行权必须在受控 runtime。

**ToolExecutor 职责**：
- 绑定 runtime user/session/run 身份，不相信模型传入身份
- 执行前检查 provider budget、policy 和工具可用性
- 记录 state、tool history、event、trace
- 统一调用 registry/tool，捕获异常并返回结构化 `ToolResult`
- 按 retry/recovery policy 决定重试、partial、skip 或 stop

**失败处理速记**：

| 错误 | 处理 |
| --- | --- |
| unknown tool / invalid input | reject，不进入 executor |
| timeout / rate limit | 幂等且 policy 允许时 retry |
| auth failed / unconfigured | 不重试，stop 或降级 |
| budget exceeded | 执行前阻断；关键步骤 stop，可选步骤 partial |
| optional step failed | skip 或 `continue_with_partial_result` |
| 已有成功 observation | 不重复失败工具，基于已有结果回答或换工具 |

**Observation 要求**：脱敏、结构化、带 `error_code` / `error_message` / `next_step_hint`，不要暴露 raw exception、API key、Authorization、provider raw response 或 base64 大 payload。

**面试金句**：
> 工具失败不是异常字符串，而是下一轮推理的数据；它必须结构化、脱敏，并带恢复提示。

---

## 🔁 ReAct 工具调用循环篇

### Tool Calling Loop 设计

**核心链路**：

```text
LLM decision
  -> ActionValidator
  -> ToolExecutor
  -> ToolResult
  -> ToolObservation
  -> next assistant iteration or final answer
```

**参数校验清单**：
- 工具是否存在，tool input 是否是 JSON object
- schema 类型、required、枚举、范围、`additionalProperties`
- 业务必需参数、前置状态、权限、风险等级
- 敏感字段、越权身份字段、危险动作参数

**失败处理清单**：
- 参数错误：rejected observation，让模型修正或追问
- timeout / rate limit：幂等且 policy 允许时 retry
- auth / unconfigured / permission denied：不重试，stop 或降级
- budget exceeded：执行前阻断，关键步骤 stop，可选步骤 partial
- 高风险动作：pending action + human approval + audit

**幂等性**：对付款、发邮件、删除数据等副作用工具使用 idempotency key、执行记录、请求去重和审批状态，避免重复执行。

**停止条件**：不能只靠 LLM；runtime 必须有 max iterations、token/cost budget、超时、重复失败检测、terminal tool 和 cancel/interrupt。

**Prompt injection 防护**：工具结果是不可信 observation，不是 instruction；高风险动作由代码层权限、审批和审计兜底。

**质量评估**：tool selection accuracy、参数正确率、任务完成率、错误恢复率、无效调用率、高风险拦截率、latency、token cost、retry rate。

**面试金句**：
> LLM 可以决定下一步意图，但工具执行边界必须由代码、schema、权限和审计来控制。

> 工具结果是 observation，不是 instruction；LLM 可以读它，但不能让它改写系统规则或越权调用工具。

---

## 💡 万能套话

### 工具调用设计三大原则

1. **不信任原则**：永远假设 LLM 会传错参数、调错工具、跳步骤，系统必须有校验
2. **单一数据源原则**：约束条件只写一份，LLM 读的和系统校验的必须是同一份
3. **分层防御原则**：Prompt 约束是第一层，代码校验是第二层，状态机是第三层
