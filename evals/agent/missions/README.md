# Agent Mission 目录

本目录用于保存面向真实用户目标的上层 Agent 任务（Mission）。Mission 通常组合多个工具、等待点和
可恢复状态，用来回答“Agent 能否完成一个完整目标”，而不是隔离验证某个基础能力。

基础能力 Task 案例已移除；当前只保留面向完整用户目标的 Mission。每个可运行 Mission 复用：

```text
<mission_id>/
  task.json
  environment.py
  grader.py
  calibration.json
```

Mission 使用活动 `AgentGraphRuntime`、现有 `TaskSpec` / `RunEvidence` 契约、Environment 隔离、
隐藏 Grader、校准样本、统一 Langfuse Dataset 和固定三个 task-level canonical Score。

## 当前状态

- `evals.agent.loader` 从 `missions/` 发现当前案例；
- Mission 共用 `--inspect`、`--calibrate`、`--publish`、`--run` 和固定三个 task-level Score；
- Mission Environment 必须提供非空、只含 Rule assertion 的
  `objective_state_assertions()`；工具 outcome 与 objective state 共同合入 `tool_execution`；
- Langfuse Dataset item 保持 `task_id + request + 短 metadata`，不复制 case level、state oracle
  或 rubric；
- Mission Rule、Environment、Evidence、Dataset、Trace、Judge 或 Score 故障属于评测基础设施错误，
  CLI 退出 2，不生成或篡改 Agent Score。

当前协议权威见 [evals/README.md](../../README.md)。历史案例路线与优先级记录在
`docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md`；该 roadmap 只是历史/开发材料，
不是当前协议或设计决定的权威。
