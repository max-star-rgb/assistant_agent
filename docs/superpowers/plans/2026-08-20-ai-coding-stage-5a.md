# AI Coding Stage 5A 验证失败修复循环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在唯一顺序 `AssistantCodingGraph` 中增加最多两轮、逐轮重新审批的确定性 validation failure repair loop。

**Architecture:** 新增 `coding.repair` 纯策略层负责 eligible failure、证据投影、无进展检测与 repair HITL contract；`CodingWorkspaceService` 使用临时 Git index 计算当前及候选累计 diff digest，不修改 worktree；现有 CodingGraph 增加 `prepare_repair` 节点和 `run_validation -> prepare_repair -> inspect_and_draft` 原生回边，继续复用同一 patch/apply/gates/integration mutation lane。

**Tech Stack:** Python 3.12、Pydantic v2、LangGraph `StateGraph` / `interrupt` / `Command`、Git temporary index、pytest。

**Spec:** `docs/superpowers/specs/2026-08-20-ai-coding-stage-5a-validation-repair-loop-design.md`

## Global Constraints

- 仅 `test|lint|build` 且 `error_code="verification_command_failed"`、具有非零 `exit_code` 的 command evidence 可进入 repair。
- timeout、OOM、resource、sandbox、dependency、credential、artifact、formatter 与 cleanup failure 永不进入 repair。
- `MAX_REPAIR_ROUNDS = 2`，达到上限后结构化终止，不继续调用模型。
- repair patch 是针对当前 workspace 的增量 patch；审批展示累计 diff，并绑定 `patch_digest + workspace_diff_digest + candidate_diff_digest`。
- 每轮 repair patch 都重新进入原生 HITL；旧 patch/dependency/credential/artifact approval 不得复用。
- 每次 apply 后重新经过 dependency、credential、artifact gates；integration 只在最终 validation 通过后运行。
- repair context 只包含单个失败命令的有界、脱敏 stdout/stderr 与结构化字段，不进入标准 conversation messages。
- 生产 Runtime 仍只有 Agent Server 持有的 `AssistantRootGraph` / `AssistantCodingGraph`；不创建新 run、Repair Runtime、shell、push 或 PR 能力。
- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、offline；不调用真实 Provider 或网络。
- 临时 TDD 只放 `tests/tdd/ai-coding-repair-loop/`，不提交；设计和计划文档也不提交。

---

### Task 1: Repair contract 与 eligible failure 策略

**Files:**
- Create: `src/assistant_agent/coding/repair.py`
- Modify: `src/assistant_agent/coding/models.py`
- Test: `tests/tdd/ai-coding-repair-loop/test_repair_policy.py`

**Interfaces:**
- Consumes: `CodingVerificationResult`、`CodingCommandEvidence`。
- Produces: `MAX_REPAIR_ROUNDS: int`、`CodingRepairFailureEvidence`、`CodingRepairAttempt`、`CodingRepairApprovalContext`、`CodingRepairApprovalDecision`、`select_repairable_failure(result, repair_round)`、`render_repair_context(evidence, repair_round)`、`repair_interrupt_payload(...)`、`validate_repair_approval(...)`。

- [ ] **Step 1: 写失败测试锁定 repair 准入矩阵**

```python
@pytest.mark.parametrize("kind", ["test", "lint", "build"])
def test_only_normal_command_failure_is_repairable(kind):
    result = verification_failure(kind=kind, exit_code=1,
                                  error_code="verification_command_failed")
    evidence = select_repairable_failure(result, repair_round=0)
    assert evidence.command_id == "verify"
    assert evidence.kind == kind

@pytest.mark.parametrize("error_code", [
    "sandbox_timeout", "sandbox_oom_killed", "verification_disk_limit",
    "dependency_offline_install_failed", "artifact_scan_failed",
])
def test_infrastructure_and_resource_failures_are_not_repairable(error_code):
    assert select_repairable_failure(
        verification_failure(kind="test", exit_code=None, error_code=error_code),
        repair_round=0,
    ) is None

def test_repair_limit_is_two_rounds():
    assert select_repairable_failure(command_failure(), repair_round=2) is None
```

- [ ] **Step 2: 运行策略测试确认 RED**

Run:

```bash
PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-repair-loop/test_repair_policy.py
```

Expected: collection FAIL，缺少 `assistant_agent.coding.repair` 和 repair models。

- [ ] **Step 3: 实现冻结 Pydantic contract**

在 `models.py` 增加：

```python
class CodingRepairFailureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    command_id: str
    kind: Literal["test", "lint", "build"]
    exit_code: int
    error_code: Literal["verification_command_failed"]
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: str = Field(max_length=16_777_216)
    stderr: str = Field(max_length=16_777_216)
    truncated: bool = False

class CodingRepairAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    round: int = Field(ge=1, le=2)
    failure_output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["passed", "failed"]

class CodingRepairApprovalContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    repair_round: int = Field(ge=1, le=2)
    patch_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cumulative_diff_preview: str = Field(max_length=32_000)

class CodingRepairApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    decision: Literal["approve", "reject", "respond"]
    patch_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_diff_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_diff_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response: str | None = Field(default=None, max_length=4_000)
```

- [ ] **Step 4: 实现纯策略函数**

`select_repairable_failure` 必须要求 `result.status == "failed"`，从 evidence 中筛出且只筛出一个符合准入条件的失败项；`repair_round >= 2` 返回 `None`。`render_repair_context` 使用固定模板，仅投影 contract 字段并明确剩余轮数，不接收任意宿主 metadata。`validate_repair_approval` 对 approve 强制比较三个 digest，对 reject/respond 保持现有语义。

- [ ] **Step 5: 运行 Task 1 测试确认 GREEN**

Run: Task 1 Step 2 同一命令。

Expected: PASS。

- [ ] **Step 6: 提交生产 contract，保持 TDD 未提交**

```bash
git add src/assistant_agent/coding/models.py src/assistant_agent/coding/repair.py
git commit -m "feat: define coding validation repair contracts"
```

---

### Task 2: 临时 Git index 累计 diff 与 no-progress 绑定

**Files:**
- Modify: `src/assistant_agent/coding/workspace.py`
- Modify: `src/assistant_agent/coding/repair.py`
- Test: `tests/tdd/ai-coding-repair-loop/test_repair_preview.py`

**Interfaces:**
- Consumes: `CodingWorkspace`、已重新验证的 `CodingPatchValidation`、`CodingRepairAttempt` history。
- Produces: `CodingWorkspaceService.preview_repair_patch(workspace, validation, repair_round) -> CodingRepairApprovalContext`、`ensure_repair_progress(context, proposal, history) -> None`。

- [ ] **Step 1: 写失败测试锁定无宿主 mutation 的累计 preview**

```python
def test_preview_binds_current_and_candidate_cumulative_diff(workspace_service, workspace):
    apply_first_patch(workspace_service, workspace)
    before = workspace_service.diff(workspace)
    validation = workspace_service.validate_patch(workspace, REPAIR_PATCH, "repair")
    context = workspace_service.preview_repair_patch(workspace, validation, repair_round=1)
    assert context.patch_digest == validation.proposal.patch_digest
    assert context.workspace_diff_digest != context.candidate_diff_digest
    assert "first change" in context.cumulative_diff_preview
    assert "repair change" in context.cumulative_diff_preview
    assert workspace_service.diff(workspace) == before
```

再覆盖 new file、delete、路径漂移、patch digest 漂移、重复历史 patch digest，以及 candidate digest 等于 current digest 时的 `coding_repair_no_progress`。

- [ ] **Step 2: 运行 preview 测试确认 RED**

```bash
PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-repair-loop/test_repair_preview.py
```

Expected: FAIL，缺少 `preview_repair_patch`。

- [ ] **Step 3: 用 temporary index 计算累计 diff**

在 workspace 独占锁内：

```text
temp index
  -> git read-tree HEAD
  -> GIT_INDEX_FILE=<temp> git add -A --
  -> git diff --cached --no-ext-diff --no-color HEAD --   # current
  -> GIT_INDEX_FILE=<temp> git apply --cached --whitespace=nowarn -
  -> git diff --cached --no-ext-diff --no-color HEAD --   # candidate
```

patch 只通过 stdin；Git env 沿用现有净化逻辑并额外设置 `GIT_INDEX_FILE`。运行前重新校验 proposal patch digest、base commit 和 base file digests。current/candidate 完整输出只用于 SHA-256；公开 preview 截断到 32,000 字符。临时 index 位于受管 workspace metadata root，`finally` 删除；任何 Git 或 cleanup 不确定状态返回稳定 `CodingWorkspaceError`。

- [ ] **Step 4: 实现 no-progress 策略**

`ensure_repair_progress` 在以下任一情况抛 `ValueError("coding_repair_no_progress")`：patch digest 已存在于 history、current/candidate diff digest 相同、repair round 不匹配。

- [ ] **Step 5: 运行 Task 2 测试确认 GREEN**

Run: Task 2 Step 2 同一命令。

Expected: PASS，且 worktree diff 在 preview 前后完全一致。

- [ ] **Step 6: 提交生产 preview 实现**

```bash
git add src/assistant_agent/coding/workspace.py src/assistant_agent/coding/repair.py
git commit -m "feat: bind coding repair to cumulative diff"
```

---

### Task 3: CodingGraph repair preparation、临时 context 与 repair HITL

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Test: `tests/tdd/ai-coding-repair-loop/test_repair_graph.py`

**Interfaces:**
- Consumes: Task 1/2 repair policy、models 与 `preview_repair_patch`。
- Produces: `prepare_repair` node；`approval_origin="repair"`；repair state channels；repair approval resume digest validation。

- [ ] **Step 1: 写失败测试锁定 Graph 结构和 state 清理**

```python
def test_validation_failure_routes_to_single_repair_lane(graph):
    nodes = set(graph.get_graph().nodes)
    assert "prepare_repair" in nodes
    assert ("prepare_repair", "inspect_and_draft") in set(graph.builder.edges)

def test_prepare_repair_clears_every_stale_authorization(...):
    update = invoke_prepare_repair(state_with_eligible_failure_and_old_approvals())
    assert update["repair_round"] == 1
    assert update["proposal"] is None
    assert update["approval_status"] is None
    assert update["dependency_plan"] is None
    assert update["credential_request"] is None
    assert update["artifact_ingress_plan"] is None
```

增加 scripted inspect agent 断言 repair `HumanMessage` 只存在于本轮 ainvoke 输入，返回 state messages 不包含该临时消息；stdout/stderr 已有界且无宿主路径。

- [ ] **Step 2: 运行 Graph 测试确认 RED**

```bash
PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-repair-loop/test_repair_graph.py
```

Expected: FAIL，缺少 `prepare_repair` node/state。

- [ ] **Step 3: 扩展 CodingState**

新增：

```python
approval_origin: NotRequired[Literal["model", "formatter", "repair"]]
repair_round: NotRequired[int]
repair_status: NotRequired[Literal["pending", "active", "passed", "exhausted", "no_progress"]]
repair_failure_evidence: NotRequired[CodingRepairFailureEvidence | None]
repair_history: NotRequired[Annotated[list[CodingRepairAttempt], operator.add]]
repair_approval_context: NotRequired[CodingRepairApprovalContext | None]
```

- [ ] **Step 4: 增加 prepare_repair 与临时模型 context**

`prepare_repair_node` 只消费 `repair_failure_evidence`，递增 round，并原子清空 proposal/validation/approval、applied result、三类治理 plan/request/status、integration pending state和上一轮 repair approval context。`inspect_and_draft_node` 对 active repair 复制 state messages、追加 `HumanMessage(render_repair_context(...))` 后调用 inspect agent，但只返回 agent 新产生的 messages。

- [ ] **Step 5: 在 proposal validation 中计算 repair approval context**

当 `repair_status == "active"` 时设置 `approval_origin="repair"`，调用 `preview_repair_patch` 和 `ensure_repair_progress`。失败返回 `coding_repair_no_progress` terminal；成功保存 `repair_approval_context`。

- [ ] **Step 6: 扩展 approval node 的 repair 分支**

repair 时 interrupt 使用 `repair_interrupt_payload`，展示累计 preview；resume 使用 `CodingRepairApprovalDecision`，随后重新 resolve workspace、重新 validate incremental patch、重新计算 `CodingRepairApprovalContext` 并比较三个 digest。任何漂移进入 `approval_digest_mismatch`；approve 才 goto `apply_patch`。普通 model/formatter approval 行为不变。

- [ ] **Step 7: 运行 Task 3 测试确认 GREEN**

Run: Task 3 Step 2 同一命令。

Expected: PASS。

- [ ] **Step 8: 提交生产 Graph preparation/HITL**

```bash
git add src/assistant_agent/native_agent/state.py src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: add digest bound coding repair approval"
```

---

### Task 4: 两轮 validation 回边、终态与治理 gates

**Files:**
- Modify: `src/assistant_agent/coding/models.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Test: `tests/tdd/ai-coding-repair-loop/test_repair_lifecycle.py`

**Interfaces:**
- Consumes: Task 3 repair state/HITL。
- Produces: deterministic `after_run_validation` repair route；`CodingTerminalResult.repair_status` / `repair_history`；成功/失败 attempt history。

- [ ] **Step 1: 写失败生命周期测试**

使用 scripted validation service 和 scripted inspect agent 覆盖：

```python
def test_first_repair_success_reaches_integration_only_after_revalidation(...):
    result = run_graph(validation_results=[failed_command(), passed()])
    assert result["coding_result"].status in {"applied", "merged"}
    assert result["coding_result"].repair_status == "passed"
    assert len(result["coding_result"].repair_history) == 1

def test_two_failed_repairs_stop_without_third_model_call(...):
    result = run_graph(validation_results=[failed_command(), failed_command(), failed_command()])
    assert result["coding_result"].repair_status == "exhausted"
    assert inspect_agent.repair_calls == 2

def test_resource_failure_never_calls_repair_agent(...):
    result = run_graph(validation_results=[oom_failure()])
    assert result["coding_result"].repair_status is None
    assert inspect_agent.repair_calls == 0
```

另断言每个 repair apply 后路径仍依次经过 `plan_dependencies -> plan_credentials -> plan_artifacts -> run_validation`；受治理 manifest 改变时需要新 interrupt，旧 approval 不能复用。

- [ ] **Step 2: 运行 lifecycle 测试确认 RED**

```bash
PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-repair-loop/test_repair_lifecycle.py
```

Expected: FAIL，validation failure 仍直接 terminal。

- [ ] **Step 3: 扩展 terminal contract**

为 `CodingTerminalResult` 增加：

```python
repair_status: Literal["passed", "exhausted", "no_progress"] | None = None
repair_history: tuple[CodingRepairAttempt, ...] = ()
```

保持 extra forbid；对 history 增加 list-to-tuple validator。

- [ ] **Step 4: 实现 deterministic validation route**

`run_validation_node` 对 failed result 调用 `select_repairable_failure`：

- eligible：不写 terminal，保存 failure evidence 与 `repair_status="pending"`；
- ineligible：沿用当前 failed terminal；
- 当前 repair round 失败：先把本轮 `CodingRepairAttempt(status="failed")` 追加 history，再决定下一轮或 exhausted；
- repair validation passed：追加 `status="passed"` attempt，清除 repair failure，继续 integration/applied terminal。

`after_run_validation` 在 `repair_status == "pending"` 时返回 `prepare_repair`；formatter approval 优先级保持现有逻辑；只有 passed 才返回 `create_commit` 或 `summarize`。

- [ ] **Step 5: 保持完整 gates 与累计 changed paths**

repair apply 后必须走现有 `plan_dependencies` edge；`approved_changed_paths` 的 unique reducer累积所有轮次 changed paths。`prepare_repair` 清除旧 gates 后，新 patch 若未触发 plan 则标记 `not_required`，若触发则重新 interrupt。integration service 收到累计 changed paths 和全部 verification evidence。

- [ ] **Step 6: 运行 Task 4 测试确认 GREEN**

Run: Task 4 Step 2 同一命令。

Expected: PASS。

- [ ] **Step 7: 运行 Stage 5A 全部 TDD**

```bash
PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-repair-loop
```

Expected: PASS。

- [ ] **Step 8: 提交生产 lifecycle**

```bash
git add src/assistant_agent/coding/models.py src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: retry deterministic coding validation failures"
```

---

### Task 5: Authority、core invariant、回归与安全审查

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify if required: `docs/authority.toml`
- Test: `tests/tdd/ai-coding-repair-loop/`

**Interfaces:**
- Consumes: 完整 Stage 5A Graph/control contracts。
- Produces: 当前 authority、`LOOP-001` 稳定节点断言、最终验证证据。

- [ ] **Step 1: 更新 runtime authority**

记录 `run_validation -> prepare_repair` 原生回边、两轮上限、eligible failure、临时 evidence context、每轮 patch HITL、完整 gates 与单一 mutation lane。明确基础设施错误不可修复，repair 不创建第二套 Runtime/run。

- [ ] **Step 2: 更新 Agent Server authority**

记录 checkpoint 只保存结构化 repair evidence/history/digests，不保存完整日志、宿主路径或进程对象；interrupt/resume 由 Agent Server 原生拥有；integration 仅在最终通过后执行。

- [ ] **Step 3: 更新 LOOP-001 和既有 core test**

在 `LOOP-001` 增加“eligible validation failure 最多两轮返回同一 digest-bound approval/apply/gates 闭环”。既有 `test_parent_graph_has_fast_planning_and_coding_native_branches` 只增加：

```python
assert "prepare_repair" in coding_nodes
```

不新建 core 文件，不在 core 中导入 repair feature implementation 或断言完整 prompt。

- [ ] **Step 4: 运行文档权威检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: `valid: true`，无 errors。

- [ ] **Step 5: 运行 Stage 5A、受影响 core 和历史 coding 回归**

分别以独立 pytest 进程运行，避免临时 feature `conftest` 模块名碰撞：

```bash
PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-repair-loop

PYTHONPATH=$PWD/src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

再从主工作目录复制当前 Stage 1-4B3 临时 coding TDD 到 `*-5a-regression`，逐目录独立运行。Stage 4A 中对
`CodingSandboxRequest` 旧字段集合的精确断言继续标记为已被 4B1-4B3 contract 替代，不把它解释为回归。

- [ ] **Step 6: 执行独立安全审查**

审查重点：repair eligibility 不能由模型控制、基础设施错误不进入 loop、两轮硬上限、临时 context 不污染 messages、
旧 approval/gates 清除、累计 diff digest resume 重算、no-progress、integration 不提前、checkpoint 不含宿主路径。
修复全部 Critical/Important finding 并重跑受影响测试。

- [ ] **Step 7: 提交 authority/core 同步**

```bash
git add docs/runtime-event-stream-architecture.md docs/agent-server-architecture.md \
  docs/authority.toml tests/core/INVARIANTS.md \
  tests/core/integration/test_runtime_lifecycle.py
git commit -m "docs: document coding validation repair loop"
```

- [ ] **Step 8: 合并后验证唯一 8089 hot reload**

本地合并到 `cqy` 后，在主工作目录重跑 Stage 5A TDD、默认 core、authority validator，并仅作为客户端请求
`http://127.0.0.1:8089/ok`。不得启动第二套 dev server；若 8089 未运行则报告，不自行另起端口。

## 完成汇报格式

```text
完成：AI Coding Stage 5A 验证失败修复循环。
Core invariant: LOOP-001 changed to include the bounded native coding repair loop.
Tests: added tests/tdd/ai-coding-repair-loop for temporary RED/GREEN; user may delete the directory manually.
Validation: <列出实际命令、通过数和唯一已知过期断言>。
Provider: 未调用真实 Provider；全部验证使用 mock/offline。
Review: <Critical/Important closure 与残余风险>。
Limitations: 不修复基础设施错误；最多两轮；无并行分析、独立 review graph、push/PR 或跨 run 长任务恢复。
```
