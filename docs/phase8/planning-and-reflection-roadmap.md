# Planning and Reflection Roadmap

## 背景

Phase 8A 只完成 assistant-driven tool loop。

第一版只支持：

```text
final_answer
tool_call
ask_followup
execute_tool loop
max loop guard
```

Planning 和 reflection 是后续增强，不应该和 Phase 8A 一次性混在一起。

---

## Phase 8B：Parallel Execution Strategies

### 目标

新增与 ReAct 平行的 `plan_and_solve` 显式执行策略。调用方选择 strategy，默认仍为 ReAct。

### 图结构

```text
START
  ↓
load_memory
  ↓
resolve_execution_strategy
  ├─ react_subgraph
  └─ plan_and_solve_subgraph
  ↓
response_handoff / compose_response
  ↓
save_memory
  ↓
END
```

### Plan-and-Solve 子图

```text
planner
  ↓
validate_plan
  ↓
plan_controller
  ├─ execute_one_step -> plan_controller
  ├─ replan -> planner
  ├─ ask_followup -> response
  └─ final_answer -> response
```

### 推荐字段

```text
execution_strategy
plan
plan_status
current_step_id
plan_revision_count
outputs_by_step
tool_observations
```

### 关键原则

ReAct 和 Plan-and-Solve 只负责“谁决定下一步”，不能复制工具执行底座。

Plan-and-Solve 中，规划、重规划、下一步选择由 LLM 完成；代码只负责 schema、工具白名单、预算、依赖、状态、trace 和调度。

每次只执行一个步骤。每一步完成后必须把 ToolObservation 交还给 controller LLM。

真实 LLM 路径不要复用旧 `RuleBasedTaskPlanner`。

### 不做

```text
不做复杂长期任务恢复
不做并发工具执行
不做自动部署
不调用真实 Provider
不做 auto strategy router
不把 create_plan 伪装成工具
不本地 for-loop 自动执行完整计划
```

### Strategy Eval / Review

Phase 8B 的稳定性评估新增独立 `strategy` suite：

```text
strategy_react_default
strategy_plan_and_solve_multistep
strategy_plan_unknown_tool_rejected
strategy_plan_replan_after_tool_failure
```

该 suite 使用 scripted chat adapter 模拟 planner/controller 的结构化 LLM 输出，通过正常 `AgentGraphRuntime` 跑图。评估项包括：

```text
execution_strategy contract
AgentRunResponse contract
tool sequence
plan_status
plan_revision_count
controller/planner call count
decision trace
trace node path
error code
```

它不调用真实 Provider，也不使用旧 `RuleBasedTaskPlanner` 生成真实路径计划。

---

## Phase 8C：Reflection Follow-up

### 目标

让系统在工具失败、低置信度、循环接近上限时进入 reflection。

### 触发条件

```text
tool failure
invalid tool input
unknown tool
low confidence
loop limit approaching
missing required output
```

### 推荐新增 schema

```text
ReflectionResult
```

### Reflection 可输出

```text
revise_decision
ask_followup
final_answer_with_caveat
stop_with_error_summary
```

Reflection 不允许直接执行工具。

---

## 推荐策略

Phase 8A 完成并稳定后，再单独执行 Phase 8B。

Phase 8B 稳定后，再单独执行 Phase 8C。

不要一次性让 Codex / Claude Code 同时实现 Phase 8A、8B、8C。
