# Task 026 LangGraph 多步循环执行

## Goal

让多步任务由 LangGraph 显式循环节点执行，而不是依赖同步 workflow for-loop。

## Read first

- `docs/23-langgraph-loop-planner.md`
- 当前 planner
- 当前 graph runtime
- 当前 TaskPlan / PlanStep schema

## Scope

实现 LangGraph loop：

```text
plan_steps
  ↓
select_next_step
  ↓
execute_step
  ↓
should_continue?
      ├─ continue → select_next_step
      └─ finish   → compose_response
```

## Requirements

- 使用 LangGraph conditional edge 实现循环。
- 每次只执行一个 PlanStep。
- tool_results 按 step 写回 state。
- 支持失败记录。
- 默认 MockAdapter。
- 不调用真实服务。

## Tests

新增或更新：

```text
tests/test_langgraph_multistep_loop.py
```

覆盖：

```text
找视频里的鞋子，比较价格，再生成海报
```

期望工具顺序：

```text
vision_understanding
product_search
price_compare
image_generation
```

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 027。
