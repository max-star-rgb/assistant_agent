# 76 Phase 5F 路线图：Hybrid Intent Router & Planner Quality

## 背景

Phase 5A 已完成 Assistant Capability Routing Baseline。

Phase 5B 已完成：

```text
direct_chat
image_generation
```

Phase 5C 已完成：

```text
product_search
price_compare
```

Phase 5D 已完成：

```text
render_3d
```

Phase 5E 已完成：

```text
End-to-End Demo Flow
Response Composer Quality
Capability Output Contract
Demo Runner
Eval Suite Layering
```

当前系统已经具备完整 Assistant capability baseline：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
multi_step_orchestration
```

下一阶段的主要问题不是继续扩 Provider，而是提升：

```text
用户意图理解
任务规划质量
缺失信息识别
多步任务拆解
追问质量
```

因此 Phase 5F 聚焦：

```text
Hybrid Intent Router & Planner Quality
```

## Phase 5F 总目标

构建一个安全、可测试、可替换的混合意图识别和规划层：

```text
User Request
  ↓
Rule Router
  ↓ 低置信 / 模糊
LLM Intent Router Adapter
  ↓
IntentDecision
  ↓
Capability Validator
  ↓
Task Planner
  ↓
LangGraph Execution
```

核心原则：

```text
规则优先
LLM 可选兜底
结构化输出
Validator 强校验
LangGraph 执行
默认离线
不默认调用真实 LLM
```

## 为什么不是纯规则

规则路由稳定、便宜、可解释，但面对自然语言表达会逐渐变脆。

例如：

```text
帮我把这个东西弄得更适合卖
```

可能对应：

```text
image_understanding → image_generation
```

也可能对应：

```text
product_search → price_compare → image_generation
```

再比如：

```text
按我之前喜欢的风格，给这个做个展示
```

可能对应：

```text
memory_retrieval → image_generation
```

或：

```text
memory_retrieval → render_3d
```

纯规则难以覆盖这些模糊、多意图、隐含上下文表达。

## 为什么不是纯 LLM

纯 LLM Router 有风险：

- 成本更高。
- 延迟更高。
- 默认不能离线。
- 输出可能不稳定。
- 可能过度调用工具。
- 可能在缺少输入时错误执行。
- 可能错误触发真实 Provider。

因此 LLM 只能作为候选决策器，不能直接拥有执行权。

## 正确边界

LLM 可以输出：

```text
intent candidate
plan candidate
missing input candidate
reason
confidence
```

但必须经过：

```text
IntentDecision schema
CapabilityValidator
TaskPlanner
LangGraph Runtime
```

才能执行。

## Phase 5F 不做什么

本阶段不做：

- 默认调用真实 LLM。
- 让 LLM 直接调用工具。
- 替换掉 Rule Router。
- 新增真实 Provider。
- MCP Server。
- Skills 打包。
- 生产权限系统。
- 成本计费系统。
- 大规模重构 LangGraph。

## Phase 5F 要做什么

1. 定义统一 `IntentDecision` schema。
2. 定义 `CapabilityValidator`。
3. 让 Rule Router 输出 confidence、matched_rules、reason。
4. 增加 LLM Intent Router Adapter skeleton，默认关闭。
5. 增加 MockLLMIntentRouter，用于离线测试。
6. 改进 Planner slot filling 和 missing input 追问。
7. 增加 router eval comparison：rule vs mock_llm vs hybrid。
8. 生成 Phase 5F 审计报告。

## Phase 5F 任务顺序

```text
074 Phase 5F Hybrid Intent Router Roadmap
075 IntentDecision Schema and Capability Validator
076 Rule Router Confidence Refactor
077 LLM Intent Router Adapter Skeleton
078 Planner Quality and Slot Filling
079 Intent Router Eval Comparison
080 Phase 5F Review
```

## 默认安全边界

- 默认使用 Rule Router。
- 默认不调用真实 LLM。
- 默认 pytest 离线。
- 默认 eval 离线。
- LLM Router 默认关闭。
- Hybrid Router 只能使用 MockLLMIntentRouter，除非用户显式配置。
- LLM 输出不能直接执行工具。
- 所有 LLM 输出必须经过 Pydantic / schema 校验。
- CapabilityValidator 必须检查输入条件。
- 缺少必要输入时进入 `ask_followup`。

## Phase 5F 完成后

Phase 5F 完成后，后续可考虑：

```text
Phase 5G Provider Safety / Retry / Cost / Trace Query
Phase 5H Memory Hardening
Phase 5I MCP / Skills Packaging
```
