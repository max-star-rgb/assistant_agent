# 会议物流 Mission 完整性修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `meeting_logistics_tentative_calendar_commit` 满足 Mission 的 Environment、Grader、Calibration 和发布前完整性契约。

**Architecture:** 日历终态 oracle 只由 `MeetingLogisticsEnvironment.objective_state_assertions()` 根据 `RunEvidence.final_state` 判断；Task grader 只绑定回答质量 rubric；Calibration 用结构化工具、状态与回答证据区分正确提交和未提交反例。发布入口在写入 Langfuse 前复用本地案例完整性检查，拒绝缺少 Mission Rule、grader 或 calibration 的定义。

**Tech Stack:** Python 3.12、Pydantic、pytest、Agent eval Mission、临时 SQLite 日历、Langfuse 薄后端。

## Global Constraints

- 不修改 Langfuse UI 配置，不调用真实 Provider，不发布外部 Dataset。
- 不把 Mission state oracle 放进 grader、Task JSON 或 Langfuse metadata。
- 不修改 `tests/core`；扩展现有 Agent eval incubating 专项检查。
- 保留用户当前工作区中与 provider streaming 相关的未提交改动。

---

### Task 1: 补齐 Mission 终态契约

**Files:**
- Modify: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/environment.py`
- Modify: `evals/system/incubating/agent-eval-infrastructure/checks_meeting_logistics_mission.py`

**Interfaces:**
- Produces: `MeetingLogisticsEnvironment.initial_state(request) -> dict`
- Produces: `MeetingLogisticsEnvironment.final_state_reader(request) -> Callable[[], dict]`
- Produces: `MeetingLogisticsEnvironment.objective_state_assertions(evidence) -> dict[str, AssertionResult]`

- [x] 写失败检查，分别证明唯一暂定事件、时间地点、会议物流说明和无邀请副作用。
- [x] 运行 meeting logistics 专项检查，确认缺少方法时 RED。
- [x] 最小实现状态捕获和 Rule-only 终态断言。
- [x] 重跑专项检查确认 GREEN。

### Task 2: 补齐 Grader 与 Calibration

**Files:**
- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/grader.py`
- Create: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/calibration.json`
- Modify: `evals/system/incubating/agent-eval-infrastructure/checks_meeting_logistics_mission.py`

**Interfaces:**
- Produces: `grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)`
- Produces: `agent_eval_calibration_v3` 正反 Evidence。

- [x] 写失败检查，加载 grader/calibration 并离线回放四维人工标签。
- [x] 运行检查确认因文件缺失 RED。
- [x] 添加只负责回答质量的 grader 和两个 calibration fixtures。
- [x] 重跑检查确认 GREEN。

### Task 3: 阻止不完整案例发布

**Files:**
- Modify: `evals/agent/langfuse_backend.py`
- Modify: `evals/system/incubating/agent-eval-infrastructure/checks_agent_eval_mission_protocol.py`

**Interfaces:**
- Produces: 发布前 Git-owned Task/Mission 完整性校验；校验通过后 Dataset item 仍保持薄结构。

- [x] 写失败检查，证明缺少 Mission Rule、grader 或 calibration 时 `publish_tasks()` 在调用 Langfuse 前失败。
- [x] 运行 mission protocol 检查确认 RED。
- [x] 实现发布前完整性校验，并保留合成薄后端测试的可注入边界。
- [x] 重跑 mission protocol 检查确认 GREEN。

### Task 4: 离线验收

**Files:**
- Verify: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/**`

- [x] 运行 `--inspect --task meeting_logistics_tentative_calendar_commit`。
- [x] 运行 meeting Mission 与 mission protocol 两个专项检查。
- [x] 使用 labeled calibration judge 离线回放 calibration。
- [x] 运行完整 Agent eval infrastructure incubating 检查。
- [x] 执行 `git diff --check` 并确认没有修改 Langfuse UI、没有真实 Provider 调用。

### Task 5: 兼容 Langfuse 3.224.2 的 observation 审计

**Files:**
- Modify: `evals/agent/langfuse_backend.py`
- Modify: `evals/system/incubating/agent-eval-infrastructure/checks_agent_eval_task.py`
- Modify: `evals/README.md`
- Modify: `docs/observability-harness.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`

- [x] 用最近 remote run 的 404 证明 Observations v2 与当前 v3 write mode 不兼容。
- [x] 将持久化 Score 审计的 task observation 查询切换到 `api.legacy.observations_v1`。
- [x] 保持 Score 记录继续使用 Scores v3 API，并更新离线 fake client 契约。
- [x] 同步当前版本兼容说明。

### Task 6: 收紧 Dataset item 选择

**Files:**
- Modify: `evals/agent/langfuse_backend.py`
- Modify: `evals/system/incubating/agent-eval-infrastructure/checks_agent_eval_task.py`
- Modify: `evals/README.md`

- [x] 证明精确 Task 模式原先会选中 ARCHIVED 历史项。
- [x] 所有 Experiment 运行只选择 ACTIVE item。
- [x] 同一 `task_id` 多个 ACTIVE item 时在 Agent 执行前 fail-fast。
- [x] 同步 Dataset Items 的清理要求。
