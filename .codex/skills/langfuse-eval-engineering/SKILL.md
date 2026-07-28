---
name: langfuse-eval-engineering
description: Design, add, revise, calibrate, run, or audit assistant_agent task-centered Agent evaluations. Use for evals/agent Task, Environment, Evidence, Grader, Langfuse Dataset publishing, Experiment review, primary reward completeness, trace-informed regressions, and distinguishing Agent failures from evaluation infrastructure failures. Do not use for ordinary pytest work or real-service connectivity checks.
---

# Langfuse Eval Engineering

把一个可命名的 Agent 能力做成自包含 Task，并用受控 Environment、隐藏 Grader 和正反样本证明评测
有效。Git 中的 Task、Environment、Grader 和校准样本是回归定义权威；Langfuse 只作为 Dataset、
Experiment、Trace 和 Score 后端。

开始前完整读取 `evals/README.md`。若本 skill 与该文档或源码不一致，以文档和源码为准，并回补
本 skill。

## 1. 先选正确层

- 确定性代码契约：转到 `tests/`，使用 `$assistant-agent-development-testing`。
- 真实 Provider、Tool、Context 或 Memory 连通性：转到 `evals/system/`。
- 模型决策、工具语义、故障恢复、多轮约束或回答质量：使用 `evals/agent/`。

不要用三套机制重复验证同一事实。

## 2. 映射 Agent

只读检查公开入口、runtime、prompt、model、tools、policy、memory、依赖、状态和副作用，并搜索现有
Task、测试和已知失败。总结：

```text
Agent: 公开入口与目标
Capability: 本次唯一被测能力
Dependencies: Agent 能看到和不能看到的依赖
Effects: 读取、写入、隔离与复位
Evidence: 可独立观察的轨迹、终态和回答
```

映射时不调用真实 Provider、不安装依赖、不读取凭据。只有用户提供 Trace 来源或明确要求使用 Trace
时，才读取 [references/trace-sourcing.md](references/trace-sourcing.md)。

## 3. 选择一个 capability

如果用户尚未指定，提出二到三个彼此不同的 capability。每个方向给出真实用户请求、它能区分的
Agent 行为和所需环境。推荐一个并等待用户选择；不要把文案变化当成新能力。

## 4. 先批准 Environment

选择后完整读取 [references/task-design.md](references/task-design.md) 和
[references/grader-audit.md](references/grader-audit.md)。实施前用不超过 150 字说明：

```text
Task: 用户请求与唯一 capability
Environment: active runtime、真实/冻结/模拟依赖、可见工具
Effects: 状态位置、隔离、复位和真实调用
Evidence: grader 可独立检查的轨迹、终态和回答
```

等待用户批准。真实 Provider、真实工具和写操作仍必须通过仓库规定的显式开关。

## 5. 实现自包含 Task

每次只实现一个 `evals/agent/tasks/<task_id>/`：

1. `task.json` 只保存用户请求、capability、environment/grader 入口和短 tags；
2. `environment.py` 使用活动 `AgentGraphRuntime`，控制依赖、工具可见性、初始状态、隔离和复位；
3. `grader.py` 不向 Agent 暴露 rubric 或 oracle，基于结构化 Evidence 做任务局部判断；
4. `calibration.json` 至少含一个正确样本和一个可信但错误的样本；
5. Suite 只做 Task ID 选择，不拥有 Environment 或评分逻辑；
6. Langfuse Dataset item 只发布 `task_id + request + 短 metadata`，不复制 grader、依赖契约或长 oracle；
7. 若活动 runtime 无法表达 Environment，只增加薄适配，不把工具选择、重试或最终回答移出 Agent。

Task 不得靠 expected answer 文本匹配通过；工具行为以 Trace 和状态证据为准。

## 6. 校准、运行、审计

顺序固定：

1. `--inspect` 检查 Task 和 Environment，不联网；
2. pytest 验证 loader、Environment、Evidence、Grader 和薄 Langfuse backend；
3. `--calibrate` 直接把 grader 跑在人工标注的正反证据上；
4. `--publish` 显式发布所选 Task；
5. `--run` 执行真实 Experiment。

运行后检查 Agent 输入、工具暴露、Validator/Tool Trace、依赖结果、状态变化、最终回答、grader 理由和
`agent_eval.reward`。每个 Task 只有一个主要门槛分数；`agent_eval.check.*` 只用于定位失败。

Agent 行为不满足任务时退出 1。凭据、Trace 导出、Dataset、Judge、证据解析或 Score 缺失属于评测
基础设施错误，退出 2，不能伪装成 Agent 通过或失败。

## 7. 复盘

报告 Task 路径、Environment 边界、校准结果、运行命令、主要 reward、诊断检查、基础设施状态和
限制。请用户批准、修订、删除或选择下一个 capability。

## 不变量

- Git 定义回归任务；Langfuse 保存协作数据和运行结果。
- 一个 Task 只验证一个可命名 capability。
- Environment 拥有依赖和状态，Task 输入不描述测试机关。
- Grader 对 Agent 隐藏，并先用正反样本证明能区分结果。
- 一个主要 reward 决定通过；诊断分数不形成另一套通过规则。
- Trace 用于发现问题和提供证据，不直接充当正确答案。
- pytest 保持 mock/local/offline；真实 Provider 不得静默回退 mock。
- 不提交凭据、原始生产 Trace、真实用户数据或评测运行生成物。
