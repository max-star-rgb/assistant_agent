# Agent Mission 目录

本目录用于保存面向真实用户目标的上层 Agent 任务（Mission）。Mission 通常组合多个工具、等待点和
可恢复状态，用来回答“Agent 能否完成一个完整目标”，而不是隔离验证某个基础能力。

## 与 `tasks/` 的关系

- `evals/agent/tasks/`：基础能力 Task，例如缺失输入追问、工具失败恢复、跨工具证据使用和受控写入；
- `evals/agent/missions/`：上层 Mission，例如资格考试报名准备、报销材料包准备和跨城出行异常调整。

两类评测只做组织分层，不建立第二套运行协议。每个可运行 Mission 复用：

```text
<mission_id>/
  task.json
  environment.py
  grader.py
  calibration.json
```

Mission 使用活动 `AgentGraphRuntime`、现有 `TaskSpec` / `RunEvidence` 契约、Environment 隔离、
隐藏 Grader、校准样本、统一 Langfuse Dataset 和固定四项 Score。

## 当前状态

- `evals.agent.loader` 同时发现 `tasks/` 与 `missions/`，并拒绝跨目录重复 ID；
- Task 与 Mission 共用 `--inspect`、`--calibrate`、`--publish`、`--run` 和固定四项 Score；
- Mission Environment 必须提供非空、只含 Rule assertion 的
  `objective_state_assertions()`；工具 outcome 与 objective state 共同合入 `tool_execution`；
- Langfuse Dataset item 保持 `task_id + request + 短 metadata`，不复制 case level、state oracle
  或 rubric；
- Mission Rule、Environment、Evidence、Dataset、Trace、Judge 或 Score 故障属于评测基础设施错误，
  CLI 退出 2，不生成或篡改 Agent Score。

当前协议权威见 [evals/README.md](../../README.md)。历史案例路线与优先级记录在
`docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md`；该 roadmap 只是历史/开发材料，
不是当前协议或设计决定的权威。
