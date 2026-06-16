# 78 Rule Router Confidence Refactor

## 目标

保留现有规则路由优势，但让规则路由输出更加结构化、可比较、可回退。

当前规则式 intent router 应从简单返回 intent，升级为返回：

```text
IntentDecision
confidence
matched_rules
reason
missing_inputs
```

## 为什么需要 confidence

Hybrid Router 需要知道：

```text
规则是否足够确定？
是否需要 LLM fallback？
是否应该 ask_followup？
```

例如：

```text
生成一张日系极简海报
```

规则应高置信：

```text
intent=image_generation
confidence=0.95
```

而：

```text
帮我把这个弄得更适合卖
```

规则可能低置信：

```text
intent=multi_step_orchestration
confidence=0.45
```

这时可以进入 LLM Router 或 ask_followup。

## matched_rules

记录命中的规则名，便于调试：

```text
generate_image_keywords
product_search_keywords
price_compare_keywords
render_scene_keywords
memory_reference_keywords
media_understanding_keywords
```

## 推荐结构

```python
RuleMatch(
    rule_name="generate_image_keywords",
    intent="image_generation",
    confidence=0.95,
    reason="用户明确要求生成图片"
)
```

## 合并规则

如果多个规则命中：

```text
image_understanding + product_search + price_compare
```

则应生成：

```text
primary_intent=multi_step_orchestration
plan_steps=[...]
confidence=综合置信度
```

## 低置信阈值

建议配置：

```text
RULE_ROUTER_HIGH_CONFIDENCE=0.85
RULE_ROUTER_LOW_CONFIDENCE=0.55
```

默认逻辑：

```text
>= 0.85：直接通过
0.55 - 0.85：可进入 hybrid fallback
< 0.55：ask_followup 或 fallback
```

## 不做什么

- 不删除现有规则。
- 不默认调用 LLM。
- 不引入真实 Provider。
- 不让规则输出直接绕过 Validator。

## 验收标准

- Rule Router 输出 IntentDecision。
- 每个决策包含 confidence。
- 每个决策包含 reason。
- 命中的规则可追踪。
- 多规则命中可形成 plan_steps。
- 默认测试离线。

## 当前实现

当前实现保留旧兼容入口：

```text
IntentDetector.detect() -> IntentResult
```

并新增结构化入口：

```text
IntentDetector.detect_decision() -> IntentDecision
```

`detect_decision()` 输出：

- `source="rule"`
- `confidence`
- `matched_rules`
- `reason`
- `plan_steps`
- `missing_inputs`

该输出会先经过 `CapabilityValidator`，再返回给调用方。默认 runtime 仍使用
`detect()`，因此默认 router 仍为 rule，且本任务不引入 LLM、不接入真实 Provider。
