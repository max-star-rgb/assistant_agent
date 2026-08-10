---
name: langfuse-eval-engineering
description: Use when designing, revising, calibrating, running, or auditing assistant_agent Task-centered Agent evaluations, Langfuse Experiments, Score completeness, trace-informed regressions, or evaluation infrastructure failures.
---

# Langfuse Eval Engineering

把一个可命名的 Agent 能力做成自包含 Task，并用受控 Environment、Git Rule、原生 Langfuse
Evaluator 和正反样本证明评测有效。Git 定义 Task、Environment、确定性 conformance Rule 与人工
校准标签；Langfuse 定义版本化语义 Evaluator，并保存 Dataset、Experiment、Trace 和 Score。

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

若案例需要证明跨工具或写操作后的客观终态，先确认该终态能由结构化 state Evidence 独立证明；只有
满足时才选择 `missions/<mission_id>/`，否则使用 `tasks/<task_id>/`。两个目录共用运行协议且 ID 全局
唯一；Mission 的差异是 Environment 必须拥有非空、Rule-only 的 `objective_state_assertions()`。

## 4. 先批准 Environment

选择后完整读取 [references/task-design.md](references/task-design.md) 和
[references/grader-audit.md](references/grader-audit.md)。实施前用不超过 150 字说明：

```text
Task: 用户请求与唯一 capability
Environment: active runtime、真实/冻结/模拟依赖、可见工具
Effects: 状态位置、隔离、复位和真实调用
Evidence: Rule/Evaluator 可独立检查的轨迹、终态和回答
```

等待用户批准。真实 Provider、真实工具和写操作仍必须通过仓库规定的显式开关。

## 5. 实现自包含 Task

每次只实现一个 `evals/agent/tasks/<task_id>/` 或有结构化目标终态的
`evals/agent/missions/<mission_id>/`：

1. `task.json` 只保存用户请求、capability、environment 入口和短 tags；旧 Task 的 `grader` 字段只作
   迁移兼容，新 Task 不再新增专属 `grader.py`；
2. `environment.py` 继承共享 `ControlledTaskEnvironment`，只实现受控依赖、registry replacement、
   必需 outcome、Task 专属 Rule 和状态 hook；共享模板使用活动 `AgentGraphRuntime`，默认装配 Agent
   在相同结构化运行条件下暴露的完整工具目录，并统一提供 `validate()` 与
   `ToolOutcomeExpectation`。特殊边界用带可读 profile 的结构化 allowlist 精确收窄目录；
3. `calibration.json` 至少含一个正确样本和一个可信但错误的样本，并由
   `load_calibration_set()` 按 `schema_version` 统一加载；
4. Suite 只做 Task ID 选择，不拥有 Environment 或评分逻辑；
5. Langfuse Dataset item 只发布 `task_id + request + 短 metadata`，不复制 case level、state
   oracle、rubric、依赖契约或长 oracle；
6. 若活动 runtime 无法表达 Environment，只增加薄适配，不把工具选择、重试或最终回答移出 Agent。

Task 不得靠 expected answer 文本匹配通过；工具行为以 Trace 和状态证据为准。

## 6. 校准、运行、审计

正式人工运行顺序固定：

1. `--inspect` 检查 Task/Mission 和 Environment，不联网，并确认案例层级及 Mission Rule 是否实现；
2. pytest 验证 loader、Environment、Evidence、Git Rule 和薄 Langfuse backend；
3. 在 PyCharm 使用 **Langfuse** Run Configuration 启动本地服务；
4. 运行 `evals/agent/sync_langfuse_dataset.py` 同步 Git Task/Mission；
5. 从 Langfuse Dataset UI 通过已签名 webhook 启动真实 Experiment，并在 UI 选择 Experiment
   Evaluator。`run_runtime_audit` 不参与 Dataset 同步或 Experiment 运行。

Langfuse Remote Custom Experiment 只允许通过签名 webhook 触发同一个 `--run` CLI。默认空 payload
运行当前 Dataset 中全部 ACTIVE 且能映射到 Git 的 Task；`task`、`suite` 和 `runName` 仅作为高级
覆盖项。UI 不能创建案例、传环境变量、扩大副作用权限或替代本地校准/审计。
具体 URL、环境变量、请求 envelope、UI 字段、代理端口和签名行为只查阅 `evals/README.md`，本 skill
不复制这些部署事实。修改 Remote Experiment 契约前必须重新做端到端验证。

运行后检查 Agent 输入、工具 Trace、依赖结果、最终回答和原生 Evaluator 理由。Experiment runner
本地只写 `assistant_agent.quality.task_conformance`；Langfuse UI 选择的 Experiment Evaluator 生成
`assistant_agent.quality.grounding` 与 `assistant_agent.quality.response_quality`。三项都是独立 BOOLEAN
task-level Score，不生成 reward 或总通过分。单工具语义质量由
`assistant_agent.quality.tool_result_quality` Live Observation Evaluator 负责。
Experiment 完成后必须通过 Observations v2 定位 `experiment-item-task`，再用 Scores v3 同时按
`trace_id + observation_id` 回查三个 Score；SDK 内存结果不能证明原生 Score 已落库。当前 4.6 部署
使用 Observations v2，3.224 legacy observation API 不再是 runner 的兼容路径。

`--run` 完整产出三个 task-level Score 后退出 0；`--calibrate` 的三维人工标签不匹配时退出 1。凭据、
Trace 导出、Dataset、Evaluator、证据解析或 Score 缺失属于评测基础设施错误，退出 2。

## 7. 复盘

报告 Task 路径、Environment 边界、校准结果、运行命令、三个 task-level Score、可用的 observation Score、基础设施状态和限制。请用户
批准、修订、删除或选择下一个 capability。

## 不变量

- Git 定义回归任务；Langfuse 保存协作数据和运行结果。
- Langfuse UI 可以触发 CLI，并拥有 Evaluation Rule 的启停与 sampling 运维状态；它不能成为 Task、
  Environment、Git Rule 或权限的事实源。
- Dataset item 的 ACTIVE/ARCHIVED 状态可以选择本次是否运行，但 ACTIVE item 必须完整映射到 Git
  Task；未知、重复或契约不一致的 item 属于基础设施错误，不能静默跳过。
- 一个 Task 只验证一个可命名 capability。
- 所有 Task 的默认 Environment 暴露完整 Agent eval 工具目录；只允许媒体、entry profile、
  durable ready-step 等运行时结构化事实收窄具体 run 的可见集合，不按 Task capability 或用户话术
  预选工具。
- 精细化 override 必须由 Environment 或受信入口通过结构化
  `metadata.tool_visibility.profile + allowed_tools` 声明，限制在已注册受控工具内，由
  `validate()` 检查并为最终可见集合声明 outcome expectation；不得写进自然语言、grader 或
  Dataset metadata，也不能扩大真实调用权限。
- Environment 拥有依赖和状态，Task 输入不描述测试机关。
- Git Rule、Evaluator prompt 和人工校准标签都对 Agent 隐藏；语义 Evaluator 先用正反样本校准。
- 三个 task-level Score 保持阳性语义和相互独立，不计算 reward 或总通过状态。
- 客观事实由 Git Rule 判定，开放语义由原生 Langfuse Evaluator 判定；两者使用稳定 canonical Score
  名称且职责不重叠。
- Langfuse comment 通过时必须列出 assertion 的 `label`，失败时展示失败 assertion 的
  `label + reason`；内部 assertion key 不得单独充当用户可见诊断。
- 可客观证明的事实必须使用 Rule；LLM Judge 只判断开放语义，不能覆盖 Rule 结果。Evaluator 故障属于
  基础设施失败。
- Runtime audit 的 Live Observation Rule 与 Experiment 触发链路分离；仓库 reconcile 不覆盖 UI 中
  已有的 `enabled/sampling`，也不创建 Experiment Rule。
- Environment validation、凭据、Evidence 和 Evaluator 故障属于基础设施状态，不计入 Agent 分数。
- 工具业务结果预期只由 Environment 声明；通用评分入口把实际终态与 oracle 的匹配写入内部
  `tool_execution` 并持久化为 `task_conformance`；Mission 还把 Environment 的 objective state Rule
  合入该维度。Task 不得重复硬编码成功、错误码或 objective Rule。
- 旧 `tool_semantics` 仅为迁移兼容；持久化的单工具语义质量使用 observation-level
  `tool_result_quality`。`grounding` 判断回答是否忠于工具结果；
  `response_quality` 使用 Task 专属 rubric 判断回答是否清晰完整地回应用户。
- Trace 用于发现问题和提供证据，不直接充当正确答案。
- pytest 保持 mock/local/offline；真实 Provider 不得静默回退 mock。
- 不提交凭据、原始生产 Trace、真实用户数据或评测运行生成物。
