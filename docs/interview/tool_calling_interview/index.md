# 工具调用面试题库

最后更新：2026-07-03

> 按模块分类，点击「详情」查看完整解答和点评

---

## 模块覆盖进度

- [x] 一、基础概念篇
- [x] 二、ToolSpec 设计篇
- [x] 三、工具执行器篇
- [x] 四、ReAct 工具调用循环篇
- [ ] 五、多工具调度篇
- [ ] 六、故障处理篇
- [ ] 七、流式工具调用篇

---

## 一、基础概念篇 🔧

### Q1. "跳过前置步骤"的工具调用幻觉问题 🔴

**我的回答**：
> 1.之前调用过搜索;记忆错误注入。2.代码强制校验，必须存在搜索工具结果;强制限定llm只有搜索成功后，显示调用比价。3.本项目toolspec设计采用了问题2的思路

**核心考点**：
- 根因：LLM 是无状态的推理机，不是有状态的执行器
- 4 种工程解决手段：状态机校验、when_not_to_use 约束、流程约束、少样本示例
- 本项目设计亮点：runtime_constraints 字段让人和机器读同一份约束

[👉 详情解答](details/01-basics.md)

### Q2. Tool/function calling、普通 API 调用和 RAG 检索的区别 🔴

**我的回答**：
> 工具调用是指 LLM 输出希望使用的工具，通常是结构化字段，代码解析并执行该工具，向 LLM 返回结果。工具调用由 LLM 决定，输入输出是结构化字段。

**核心考点**：
- Tool calling 的关键是“LLM 提出结构化调用意图，runtime 校验并执行”
- 普通 API 调用是代码驱动，RAG 是知识检索补上下文，tool calling 是模型驱动的受控能力调用
- 生产级实现必须讲清楚执行边界、validator/executor、失败 observation、重试和可观测性

[👉 详情解答](details/02-tool-calling-api-rag.md)

---

## 二、ToolSpec 设计篇 🧩

### Q3. 工具定义 / Schema 设计 🔴

**我的回答**：
> 一个好的 tool specification 应该包含tool的基本信息：name、描述、如何执行，以及结构化参数，用于工具所需要的具体参数，还有语义信息，说明为何用？何时用、何时不能用。下面一个问题无法回答。

**核心考点**：
- ToolSpec 是给模型和协议看的工具契约，不应暴露具体执行实现
- JSON schema 解决字段、类型、required、additionalProperties 这类结构问题
- 工具选择、使用边界、前置条件、状态校验、预算、重试、错误治理必须由描述、usage 规则、validator 和 executor 分层处理

[👉 详情解答](details/03-tool-spec-design.md)

---

## 三、工具执行器篇 ⚙️

### Q4. 工具执行器与失败处理 🔴

**我的回答**：
> 1.LLM返回的调用结果不一定准，需要依靠规则强行介入。2.LLM生成校验：工具是否存在，是否有API等敏感内容。3.不清楚。4.提取工具调用的名称、描述、和失败原因，给LLM

**核心考点**：
- LLM 只能提出工具调用意图，不能拥有真实执行权和异常处置权
- ToolExecutor 负责身份绑定、预算、状态记录、事件/history/trace、retry/recovery 和异常结构化
- 失败处理要按错误类型、是否可重试、步骤是否可选、是否已有部分结果来决定 retry/fallback/replan/partial/stop
- 给 LLM 的失败 observation 应该是脱敏、结构化、可恢复的摘要，不是 raw exception 或 provider 原始响应

[👉 详情解答](details/04-tool-executor-failures.md)

---

## 四、ReAct 工具调用循环篇 🔁

### Q5. Tool Calling Loop 设计 🔴

**我的回答**：
> 采用 ReAct 范式：模型选择工具，代码校验参数，代码执行返回工具结果，结果并入模型下一轮 prompt。参数校验包括 JSON 字段合法和无敏感信息；工具失败返回自然语言让 LLM 重新思考；停止条件由 LLM 判断；记录工具调用前后关键节点。

**评分**：2.5 / 5，borderline。

**核心考点**：
- ReAct loop 要把模型决策、代码校验、工具执行、结构化 observation 和下一轮推理分层
- 参数校验必须覆盖 schema、业务约束、权限、风险等级、前置状态和敏感字段，不只是 JSON 合法
- 工具失败应返回结构化、脱敏、可恢复的 observation，而不是 raw exception 或泛泛自然语言
- 停止条件要由 runtime 兜底，包括 max iterations、预算、超时、重复调用检测、terminal tool 和 cancel/interrupt
- 高风险工具要额外设计审批、幂等、审计；质量提升要用 golden dataset、trace 和线上指标衡量

[👉 详情解答](details/05-react-tool-calling-loop.md)

---

## 速查资源

- [面试精华速记版](cheat-sheet.md)
- [上一个模块：上下文工程面试题库](../context_engineering_interview/index.md)
