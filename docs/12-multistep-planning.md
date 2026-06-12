# 12 多步任务规划设计

## 目标

在 LangGraph 接入后，Agent 不应只处理单个意图，而应支持用户一次提出多个连续目标。

示例：

```text
帮我找视频里的鞋子，比较价格，然后生成一张宣传海报。
```

Agent 应拆成：

```text
1. VisionUnderstanding
2. ProductSearch
3. PriceCompare
4. ImageGeneration
5. ComposeResponse
```

## 与 Tool Router 的区别

Tool Router 解决当前一步应该调用哪个工具。

Multi-Step Planner 解决整个任务需要哪些步骤，以及步骤之间如何传递结果。

## 推荐数据结构

新增或扩展：

```python
class PlanStep(BaseModel):
    step_id: str
    tool_name: str
    input_refs: list[str] = []
    status: Literal["pending", "running", "success", "failed", "skipped"] = "pending"

class TaskPlan(BaseModel):
    steps: list[PlanStep]
    current_step_index: int = 0
```

推荐位置：

```text
src/multimodal_agent/schemas/planning.py
```

## 图执行方式

```text
plan_steps
  ↓
select_next_step
  ↓
execute_step
  ↓
has_more_steps?
      ├─ yes → select_next_step
      └─ no  → compose_response
```

## MVP 范围

先支持固定规则规划，不需要 LLM 规划。

- 如果 query 包含“找/搜索/同款/相似”：加入 product_search。
- 如果 query 包含“比价/价格/便宜”：加入 price_compare。
- 如果 query 包含“生成/海报/图片”：加入 image_generation。
- 如果 query 包含“渲染/3D/放到场景”：加入 render_3d。
- 如果有图片/视频输入：优先加入 vision_understanding。

## 验收标准

- 支持至少一个 3 步任务。
- 每一步的 tool result 能被后续步骤读取。
- 失败步骤能在 final response 中说明。
- 不调用真实外部服务，使用 MockAdapter 即可。
