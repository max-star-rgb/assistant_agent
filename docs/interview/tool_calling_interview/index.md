# 工具调用面试题库

最后更新：2026-07-01

> 按模块分类，点击「详情」查看完整解答和点评

---

## 模块覆盖进度

- [x] 一、基础概念篇
- [x] 二、ToolSpec 设计篇
- [ ] 三、工具执行器篇
- [ ] 四、多工具调度篇
- [ ] 五、故障处理篇
- [ ] 六、流式工具调用篇

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

## 速查资源

- [面试精华速记版](cheat-sheet.md)
- [上一个模块：上下文工程面试题库](../context_engineering_interview/index.md)
