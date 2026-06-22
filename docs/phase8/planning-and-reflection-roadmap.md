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

## Phase 8B：Planning Follow-up

### 目标

让 `assistant_node` 可以选择进入 `plan_node`，生成可执行计划。

### 推荐新增字段

```text
current_plan
current_step_index
plan_status
```

### 推荐新增 schema

```text
PlanStep
AssistantPlan
```

### 图结构

```text
assistant_node
  ↓
plan_node
  ↓
execute_tool
  ↓
assistant_node
```

### 关键原则

Planner 不能重新变成中心 router。

Planning 只是 assistant 可选择的 action，不是替代 assistant_node 的主控系统。

### 不做

```text
不做复杂长期任务恢复
不做并发工具执行
不做自动部署
不调用真实 Provider
```

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
