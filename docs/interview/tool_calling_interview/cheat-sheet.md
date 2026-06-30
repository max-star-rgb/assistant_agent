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

## 💡 万能套话

### 工具调用设计三大原则

1. **不信任原则**：永远假设 LLM 会传错参数、调错工具、跳步骤，系统必须有校验
2. **单一数据源原则**：约束条件只写一份，LLM 读的和系统校验的必须是同一份
3. **分层防御原则**：Prompt 约束是第一层，代码校验是第二层，状态机是第三层
