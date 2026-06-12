# 23 LangGraph 多步循环 Planner 设计

## 目标

让多步计划由 LangGraph 显式循环执行，而不是继续依赖同步 workflow 中的 for-loop。

## 目标图

```text
START
  ↓
load_memory
  ↓
detect_intent
  ↓
plan_steps
  ↓
select_next_step
  ↓
execute_step
  ↓
should_continue?
      ├─ yes → select_next_step
      └─ no  → compose_response
  ↓
save_memory
  ↓
END
```

## 核心状态字段

```text
task_plan
current_step_index
tool_calls
tool_results
errors
final_response
```

## 路由函数

```python
def should_continue(state: AgentState) -> Literal["continue", "finish"]:
    ...
```

## 要求

- 每次循环只执行一个 PlanStep。
- 执行结果写回 AgentState。
- 失败步骤必须被记录。
- 可以配置是否遇到失败继续执行。
- 默认不调用真实外部服务。

## 验收标准

- 至少一个 4 步任务由 graph loop 执行。
- 测试能验证执行顺序。
- 不再依赖 workflow for-loop 执行多步计划。
