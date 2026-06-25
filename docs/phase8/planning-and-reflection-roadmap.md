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

## Phase 8B：ReAct Plan Mode

### 目标

在同一个 ReAct assistant loop 中新增 plan mode，让 LLM 可以通过受控 action 进入计划、修订计划、按计划调用工具，并在合适时退出计划。不要新增与 ReAct 平行的 `plan_and_solve` 执行策略。

### 图结构

```text
START
  ↓
load_memory
  ↓
assistant_node
  ├─ enter_plan_mode / exit_plan_mode -> assistant_node
  ├─ tool_call -> execute_tool -> assistant_node
  └─ final_answer / ask_followup -> compose_response
  ↓
save_memory
  ↓
END
```

### Plan Mode 状态

```text
plan_mode.active
current_plan
current_step_id
plan_revision_count
plan_status
outputs_by_step
tool_observations
```

### 推荐字段

```text
plan_mode
current_plan
plan_status
current_step_id
plan_revision_count
outputs_by_step
tool_observations
```

### 关键原则

Planning 是 ReAct 内部状态，不是 runtime graph strategy。不要新增 `resolve_execution_strategy`、`plan_and_solve_graph` 或独立 planner/controller 子图。

规划、修订计划、下一步选择、退出计划都由 LLM 通过结构化 `AssistantDecision` 完成；代码只负责 schema、工具白名单、预算、依赖、状态、trace 和调度。

每次只执行一个工具调用。每一步完成后必须把 `ToolObservation` 交还给同一个 `assistant_node`，由 assistant 决定继续、修订计划、追问或最终回答。

真实 LLM 路径不要复用旧 `RuleBasedTaskPlanner`。

### 不做

```text
不做复杂长期任务恢复
不做并发工具执行
不做自动部署
不调用真实 Provider
不做 execution_strategy router
不新增 plan_and_solve graph / subgraph
不把 enter_plan_mode / exit_plan_mode 伪装成外部工具
不本地 for-loop 自动执行完整计划
```

### Plan Mode Eval / Review

Phase 8B 的稳定性评估新增独立 `plan_mode` suite：

```text
plan_mode_enter_and_exit
plan_mode_multistep_tool_loop
plan_mode_unknown_tool_rejected
plan_mode_revise_after_tool_failure
```

该 suite 使用 scripted chat adapter 模拟 assistant 的结构化 LLM 输出，通过正常 `AgentGraphRuntime` 的 assistant loop 跑图。评估项包括：

```text
AssistantDecision plan-mode contract
AgentRunResponse contract
tool sequence
plan_status
plan_revision_count
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
