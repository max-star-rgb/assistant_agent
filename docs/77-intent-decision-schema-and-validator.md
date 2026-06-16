# 77 IntentDecision Schema and Capability Validator

## 目标

统一所有 intent router 的输出结构，并在执行前进行 capability contract 校验。

不管决策来自：

```text
Rule Router
Semantic Router
LLM Router
Mock Router
Fallback Router
```

都必须输出统一结构：

```text
IntentDecision
```

## IntentDecision

建议新增或整理：

```text
src/multimodal_agent/schemas/intent_decision.py
```

当前实现落点：

```text
src/multimodal_agent/schemas/intent_decision.py
```

该文件定义 `IntentDecision`、`PlanStep` 和 `DecisionSource`。`PlanStep.tool_name`
必须匹配 capability contract 中声明的工具名，避免 LLM 或 mock router 直接指定任意工具。

推荐结构：

```python
class PlanStep(BaseModel):
    step_id: str
    capability: str
    tool_name: str | None = None
    reason: str = ""
    required_inputs: list[str] = []
    optional: bool = False

class IntentDecision(BaseModel):
    primary_intent: str
    capabilities: list[str] = []
    plan_steps: list[PlanStep] = []
    missing_inputs: list[str] = []
    confidence: float = 0.0
    source: str = "rule"
    reason: str = ""
    matched_rules: list[str] = []
    raw_output_ref: str | None = None
```

## primary_intent 枚举

至少支持：

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
ask_followup
```

## capability 枚举

至少支持：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
memory_save
ask_followup
```

## CapabilityValidator

建议新增：

```text
src/multimodal_agent/agent/capability_validator.py
```

当前实现落点：

```text
src/multimodal_agent/agent/capability_validator.py
```

Validator 当前是纯校验层：只读取 `IntentDecision` 和 `UserRequest`，不调用工具、不调用真实 Provider、不访问网络。

职责：

```text
检查 IntentDecision 是否满足 capability 输入要求
补充 missing_inputs
把不合法执行改为 ask_followup
防止 LLM 直接产生危险工具调用
```

## 校验规则

### direct_chat

要求：

```text
text exists
```

### image_generation

要求：

```text
prompt or user_query exists
```

图片不是必需。

### image_understanding

要求：

```text
image exists
```

如果缺图片：

```text
ask_followup
```

### video_understanding

要求：

```text
video exists
```

如果缺视频：

```text
ask_followup
```

### product_search

要求：

```text
query or visual_summary or video_summary exists
```

### price_compare

要求：

```text
product candidates or search query exists
```

如果没有候选商品但有 query，可以规划：

```text
product_search → price_compare
```

### render_3d

要求：

```text
scene_description or render goal exists
```

缺场景时：

```text
ask_followup
```

### memory_retrieval

要求：

```text
user_id or session_id exists
```

## Validator 输出

Validator 应返回：

```text
validated IntentDecision
```

或者：

```text
ask_followup IntentDecision
```

## 安全原则

- LLM 不能直接指定未注册工具。
- LLM 不能绕过 capability contract。
- LLM 不能触发真实 Provider。
- 缺少必要输入时不能执行。
- 低置信度应追问或 fallback。
- Validator 应可单测。

## 验收标准

- 所有 router 输出统一 IntentDecision。
- 缺图片时不会执行 image_understanding。
- 缺视频时不会执行 video_understanding。
- 缺场景时不会执行 render_3d。
- price_compare 无候选但有 query 时可自动补 product_search。
- 默认测试离线。
