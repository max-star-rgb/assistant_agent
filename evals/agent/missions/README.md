# Agent Mission 目录

本目录用于保存面向真实用户目标的上层 Agent 任务（Mission）。Mission 通常组合多个工具、等待点和
可恢复状态，用来回答“Agent 能否完成一个完整目标”，而不是隔离验证某个基础能力。

## 与 `tasks/` 的关系

- `evals/agent/tasks/`：基础能力 Task，例如缺失输入追问、工具失败恢复、跨工具证据使用和受控写入；
- `evals/agent/missions/`：上层 Mission，例如资格考试报名准备、报销材料包准备和跨城出行异常调整。

两类评测只做组织分层，不建立第二套运行协议。未来每个可运行 Mission 仍复用：

```text
<mission_id>/
  task.json
  environment.py
  grader.py
  calibration.json
```

Mission 仍使用活动 `AgentGraphRuntime`、现有 `TaskSpec` / `RunEvidence` 契约、Environment 隔离、
隐藏 Grader、校准样本、统一 Langfuse Dataset 和固定四项 Score。

## 当前状态

当前 `evals.agent.loader` 只扫描 `evals/agent/tasks/*/task.json`，本目录尚未接入 loader、suite、
publish 或 Experiment 运行。不要在未完成下列设计门槛前把 Mission 文件视为可运行评测：

1. loader 同时发现 `tasks/` 与 `missions/`，并拒绝跨目录重复 ID；
2. Dataset 和 suite 继续只使用稳定 `task_id`，不复制 Mission 私有定义；
3. Environment 能确定性证明 Mission 的目标终态和禁止副作用；
4. Mission 终态 Rule 与现有四项 Score 的归属经过独立设计并同步更新权威文档；
5. 写状态可丢弃或可复位，真实 Provider 和真实写操作仍受显式开关约束。

案例路线、优先级和当前设计决定见
`docs/development/2026-07-29-complex-agent-mission-eval-roadmap.md`。
