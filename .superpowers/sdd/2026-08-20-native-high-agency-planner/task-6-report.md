# Task 6 实施报告：原生 plan revision 与 planning HITL/checkpoint resume

## Status

已实现并完成 mock/offline 验证。

## 实现

- `admit_plan` 捕获确定性 `NativePlanAdmissionError`，只把稳定、有界的
  `admission_error` code 与 `revision_count` 写入 `PlanningState`。
- `route_after_admission` 使用 LangGraph 原生 conditional edge：失败回到同一个
  `planner`，成功清空 error 并进入 `scheduler`。
- 最多允许两次 proposal revision；第三个无效 proposal 以带稳定 `code` 的有界
  `NativePlanAdmissionError` 终止 run。
- revision 沿用现有 reducer 保存的 `PlannerEvidence`、active Skill IDs 和 reference
  grants，只覆盖 `plan_candidate`；未增加 repair ledger、DB、queue、checkpoint adapter
  或 shadow state。
- replan 的 Planner 输入只追加有界只读 JSON 投影：
  `admission_error_code`，以及既有 evidence 的 `evidence_id/tool_name/status/content/artifact_ref`；
  不注入 structured artifact、完整旧 Planner transcript、Tool schema 或 raw error，并明确避免
  重复调用已成功 Tool。
- 扩展 `CTX-001`：Planner 与 worker 阶段的非 read Tool 都在执行前原生 interrupt；
  approve 后用 `Command(resume=...)` 从 checkpoint 继续，不重放已完成的 Planner Tool/worker；
  scheduler 从 checkpointed plan/results 推导 dependent wave。fast 模式同一 write Tool 自动放行。
- 更新 `LOOP-001` 的精确 planning topology，登记 `admit_plan -> planner` 原生 revision edge；
  同步 runtime/context authority。

## TDD 证据

### RED

命令：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-high-agency-planner/test_native_revision.py
```

结果：`2 failed, 1 passed`。

- revision 用例失败于 `NativePlanAdmissionError` 从 `admit_plan` 直接逃逸；
- bounded retry 用例只观察到 1 次 Planner 调用，而不是第三次有界终止；
- 既有 worker checkpoint resume 路径通过，作为 scheduler 恢复回归基线。

新增的 `CTX-001` Planner/worker/fast HITL 场景在生产改动前已通过，说明共享 fast agent 的官方
HITL/checkpoint 行为已经存在；本任务将其升级为稳定核心契约，并加强为 Planner interrupt → worker
interrupt → dependent worker 的连续 checkpoint 恢复断言。

### GREEN

实现 state/edge 后，同一 Task 6 feature 文件结果：`3 passed`。

完整 core 首轮发现 `LOOP-001` 精确 topology 仍缺少新的 `admit_plan -> planner` edge：
`1 failed, 50 passed`。根因是稳定 Graph 拓扑契约随 native revision edge 发生预期变化；最小更新已有
断言与 invariant 后，该单测通过，最终 full core 全绿。

## 最终验证

- Task 6 定向 feature + Context + Memory：`11 passed in 3.29s`
- 完整 `tests/tdd/native-high-agency-planner`：`39 passed in 5.01s`
- 完整 mock/offline core：`51 passed in 6.46s`
- 本任务变更 Python 文件 `ruff check`：通过
- authority validator：`valid: true`，`errors: []`
- `git diff --check`：通过
- 限制项检索：未实现 `coverage_audit`、酒店语义规则、repair ledger、checkpoint adapter 或
  shadow state。

Core invariant: `LOOP-001` 更新，因为 planning Graph 新增稳定的原生 bounded revision edge；
`CTX-001` 更新，只登记 Planner/worker 非 read Tool 的稳定 HITL/checkpoint resume 与不重放行为。

Tests: 新增 `tests/tdd/native-high-agency-planner/test_native_revision.py` 临时 RED/GREEN，用户可手动删除
整个 `tests/tdd/native-high-agency-planner` feature 目录；更新现有 core 负责文件，没有把 feature 细节晋升
为新的 core invariant。

## Concerns

- 仓库级 `ruff check .` 仍命中本任务未修改文件的既有错误：
  `scripts/run_system_multimodal_embedding_eval.py:18`（E402）。直接对 `HEAD` 版本通过 stdin 运行 Ruff
  可复现同一错误；本任务相关文件定向 Ruff 全部通过，未扩大 scope 修改该脚本。
- 未调用真实 Provider；全部验证均使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，无网络或付费调用。
