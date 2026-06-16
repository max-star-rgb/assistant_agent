# 80 Planner Quality and Slot Filling

## 目标

提升多步任务规划质量，让 Agent 能更稳定地识别依赖关系、缺失输入和追问策略。

## 当前问题

多步任务中可能出现：

```text
缺少图片
缺少视频
缺少预算
缺少渲染场景
缺少商品候选
缺少用户偏好
```

Planner 应该能判断：

```text
哪些缺失必须追问
哪些可以先执行
哪些步骤可选
哪些步骤失败后可继续
```

## PlanStep

建议扩展：

```text
step_id
capability
tool_name
input_refs
required_inputs
optional
depends_on
status
reason
```

## Slot Filling

常见 slots：

### image_generation

```text
prompt
style
reference_image optional
product_context optional
```

### product_search

```text
query
category
budget_min
budget_max
brand
style
visual_summary optional
```

### price_compare

```text
products
budget
sort_by
platforms
```

### render_3d

```text
scene_description
product_ref optional
model_ref optional
style
camera_angle optional
```

### memory_retrieval

```text
user_id
session_id
reference_phrase
```

## 追问策略

### 必须追问

```text
用户要求看图但没有图片
用户要求看视频但没有视频
用户要求渲染但完全没有场景
用户表达目的不明确
```

### 可以先执行

```text
预算缺失但可以先搜索
品牌缺失但可以先搜索
图片生成风格缺失但 prompt 明确
比价没有候选但有 query，可先 search
```

## 多步依赖

示例：

```text
找图里的鞋，比较价格，再生成海报
```

正确 plan：

```text
image_understanding
  ↓
product_search
  ↓
price_compare
  ↓
image_generation
```

示例：

```text
把上次那个包放到极简客厅里看看
```

正确 plan：

```text
memory_retrieval
  ↓
render_3d
```

## Planner 不应做什么

- 不直接调用工具。
- 不绕过 Validator。
- 不默认调用真实 Provider。
- 不把缺失输入当成已有输入。
- 不把 mock 结果伪装成真实结果。

## 验收标准

- Planner 可生成依赖有序 plan_steps。
- 缺关键输入时进入 ask_followup。
- price_compare 无产品但有 query 时自动补 product_search。
- 多步失败可以 partial response。
- 默认离线。

## 当前实现

当前实现落点：

```text
src/multimodal_agent/schemas/planning.py
src/multimodal_agent/agent/planner.py
```

`TaskStep` 已支持：

- `depends_on`
- `input_refs`
- `required_inputs`
- `optional`
- `reason`

`RuleBasedTaskPlanner` 当前支持：

- query-only `price_compare` 自动补 `product_search -> price_compare`。
- `memory_retrieval -> image_generation`。
- `image_understanding -> product_search -> price_compare -> image_generation`。
- `product_search -> render_3d`。
- 图片理解缺图片、视频理解缺视频、渲染缺场景时进入 `requires_followup`。

Planner 只生成计划和追问，不调用工具、不调用真实 Provider、不调用真实 LLM。
