# Phase 8：Assistant Brain Architecture

## 阶段目标

Phase 8 的目标是把项目从：

```text
intent-router workflow
```

升级为：

```text
assistant-driven tool loop
```

也就是：

```text
chat_node 不再只是一个可选分支
assistant_node 成为中心大脑
所有工具、模型和能力服务都变成 assistant 可以调用的 action
```

## 文档结构

```text
docs/phase8/
  README.md
  assistant-loop-architecture-upgrade.md
  beta-trial.md
  memory-manager-boundary.md
  planning-and-reflection-roadmap.md

task/phase8/
  README.md
  assistant-loop-mvp.md
  planning-followup.md
  reflection-followup.md

prompt/phase8/
  run-assistant-loop-mvp.md
  run-planning-followup.md
  run-reflection-followup.md
```

## 新规范

Phase 8 开始采用以下规范：

```text
task 文件负责完整任务说明
prompt 文件只负责启动执行
```

也就是说：

```text
Read first / Scope / Requirements / Acceptance / Stop condition
```

必须写在：

```text
task/phase8/*.md
```

而不是写在 prompt 里。

prompt 只应该告诉 Codex / Claude Code：

```text
执行哪个 task
遵守 task 里的 Read first / Scope / Requirements / Acceptance
完成后停止
```

## 推荐执行顺序

```text
Phase 8A Assistant Loop MVP
  ↓
Phase 8B Parallel Execution Strategies
  ↓
Phase 8C Reflection Follow-up
```

先只执行 Phase 8A。不要在第一轮同时实现 planning 和 reflection。

## Phase 8B Strategy Boundary

Phase 8B 将 `plan_and_solve` 定义为与 ReAct 平行的显式执行策略，而不是 ReAct 内部的 `plan` action。

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

第一版只支持用户或调用方显式选择：

```text
execution_strategy = "react" | "plan_and_solve"
默认 react
```

暂不实现 `auto`，避免重新长出规则式 strategy router。

两种策略只负责“谁决定下一步”。它们必须共享：

```text
ToolSpec
ActionValidator
ToolExecutor
ToolObservation
ProviderBudget
TraceStore
MemoryManager / MemoryContext
AgentState / AgentResponse contract
```

Plan-and-Solve 约束：

```text
planner_llm -> validate_plan -> plan_controller_llm -> execute exactly one step
execute_step -> ToolObservation -> plan_controller_llm
controller 可 continue / replan / ask_followup / final_answer
```

离线评估使用 `strategy` suite 覆盖显式策略选择、多步计划、计划拒绝和失败后重规划。该 suite 通过 scripted chat adapter 模拟 LLM 结构化输出，不调用真实 Provider，也不复用旧规则 planner 生成真实路径计划。

不要实现：

```text
planner_llm -> 本地 for-loop 自动跑完整 plan -> response composer
```

## Memory Boundary

Phase 8 memory 通过 `MemoryManager` 收拢边界：

```text
Agent / Assistant Loop / Memory Tools
  -> MemoryManager
  -> MemoryStore / Retrieval / Context Builder / WritePolicy
  -> InMemoryStore / JsonlMemoryStore / future DB or external memory service
```

当前 `MemoryManager` 负责加载分层 context、显式记忆保存、重复显式记忆合并、用户画像 `user_profile` 更新和 completed-run task summary 保存。用户画像暂时复用 `memory_type=preference`、`source=user_profile`，避免新增类型破坏现有检索/排序合同。

Graph state、memory tools、memory audit API 和 beta 用户数据删除都只依赖 `MemoryManager`；`MemoryStore` 保留为 runtime 内部构造细节和底层持久化接口。

Embedding / 向量检索不是当前默认依赖。后续应作为可选 adapter 接到 `MemoryManager` / `MemoryStore` 后面，测试默认继续使用本地 deterministic 行为，真实 embedding provider 只通过显式配置启用。

### Memory Audit

Memory 审计先通过结构化 API 和薄 CLI 完成，而不是引入 Claude Code 风格 notebook：

```text
GET    /memory/users/{user_id}/items
GET    /memory/users/{user_id}/items/{memory_id}
GET    /memory/users/{user_id}/audit
DELETE /memory/users/{user_id}/items/{memory_id}
DELETE /memory/users/{user_id}/sessions/{session_id}
```

CLI：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/memory_audit.py --server http://127.0.0.1:8000 list --user-id demo_user
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/memory_audit.py --server http://127.0.0.1:8000 audit --user-id demo_user
```
