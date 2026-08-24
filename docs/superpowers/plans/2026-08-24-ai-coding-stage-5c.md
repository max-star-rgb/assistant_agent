# AI Coding Stage 5C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在最终 deterministic validation 与 commit/integration 之间增加独立、只读、checkpoint-bound 的 Code Review Graph 和原生用户决策门禁。

**Architecture:** 父 `AssistantCodingGraph` 为不同 schema 的 `AssistantCodingReviewGraph` 提供窄 wrapper；子图对最终不可变快照执行三个固定并行 reviewer，并确定性聚合结构化报告。父图绑定最终 diff、validation evidence 与 report digest，只有用户明确 approve 后才能继续 mutation lane。

**Tech Stack:** Python 3.11、Pydantic v2、LangGraph `StateGraph` / `Send` / `interrupt`、pytest、现有 Agent Server workspace snapshot 与 mock Provider。

**Spec:** `docs/superpowers/specs/2026-08-24-ai-coding-stage-5c-code-review-design.md`

## Global Constraints

- `code_review_enabled` 默认 `False`，只能由受信静态 repository configuration 决定。
- reviewer 只能访问 policy-compliant final snapshot，禁止 shell、网络、patch、validation、commit、merge 和 approval Tool。
- review 必须在成功 validation 后、任何 commit/integration 前执行。
- HITL 只接受 `approve | reject`，并绑定 workspace、generation、base、snapshot、tree、diff、validation evidence 与 report digest。
- pytest 固定 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider。
- feature 测试只进入 `tests/tdd/ai-coding-code-review/`，不自动晋升 core、不自动提交。
- periodic owner 只复用既有 analysis-snapshot cleanup，不扩大到 Git worktree/admin lifecycle。

---

### Task 1: Review 数据契约与确定性聚合

**Files:**
- Create: `src/assistant_agent/coding/review.py`
- Modify: `src/assistant_agent/coding/models.py`
- Create: `tests/tdd/ai-coding-code-review/test_review_contracts.py`

**Interfaces:**
- Produces: `REVIEW_TASK_IDS: tuple[str, ...]`
- Produces: `CodingReviewTask`, `CodingReviewEvidence`, `CodingReviewFinding`, `CodingReviewerResult`, `CodingReviewInput`, `CodingReviewReport`
- Produces: `canonicalize_review_report(review_input, results) -> CodingReviewReport`
- Consumes: Stage 5B 的 canonical JSON/digest 与 bounded-output 约定，不复用 analysis result 作为 review result。

- [ ] **Step 1: 写契约 RED 测试**

  覆盖固定有序 inventory、严格字段、severity、正整数行号、relative policy path、字段/数组/JSON 硬上限、非 bool 整数、canonical digest、排序与去重、unknown/duplicate/missing task 以及 binding mismatch。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/tdd/ai-coding-code-review/test_review_contracts.py -q`

  Expected: collection 或 assertions 因 review contracts 尚不存在而失败。

- [ ] **Step 3: 实现最小契约与聚合**

  使用 Pydantic frozen/strict models；所有 digest 基于 UTF-8 canonical JSON。`canonicalize_review_report` 精确校验三个结果的 task inventory 和全部输入 binding，按 `severity, task_id, path, line, finding_id` 排序，以 canonical evidence/semantic key 去重，并生成 `clean | findings | unavailable` 与 `report_digest`。

- [ ] **Step 4: 运行 GREEN**

  Run: Task 1 定向 pytest 命令。

- [ ] **Step 5: 提交生产代码**

  Commit only: `src/assistant_agent/coding/review.py`, `src/assistant_agent/coding/models.py`

  Message: `feat: define coding review contracts`

---

### Task 2: Final snapshot 只读 Review worker 与并行子图

**Files:**
- Modify: `src/assistant_agent/coding/review.py`
- Modify if required by existing snapshot adapter: `src/assistant_agent/agent_server/workspace.py`
- Create: `tests/tdd/ai-coding-code-review/test_review_graph.py`
- Create: `tests/tdd/ai-coding-code-review/test_review_snapshot_security.py`

**Interfaces:**
- Produces: `create_coding_review_graph(...) -> CompiledStateGraph`
- Produces: `prepare_review_tasks(state) -> dict`
- Produces: `review_workspace(state, runtime) -> dict`
- Produces: `join_review(state) -> dict`
- Consumes: 现有 content-addressed analysis snapshot service 与 policy-compliant read Tool adapter。

- [ ] **Step 1: 写子图和安全 RED 测试**

  断言 exactly three `Send` workers、每项任务一次、并发结果确定性、worker observation/result binding、clean/findings/unavailable；断言 Tool exposure 不含 shell/network/patch/validation/integration，protected path、symlink escape、oversize、non-UTF8 和 raw management metadata 不可读。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/tdd/ai-coding-code-review/test_review_graph.py tests/tdd/ai-coding-code-review/test_review_snapshot_security.py -q`

  Expected: graph factory/worker 尚不存在或缺少安全约束。

- [ ] **Step 3: 实现最小只读子图**

  复用 snapshot store 的内容寻址与 policy view；为 review 定义独立只读 Tool profile 和固定 prompt。子图使用 per-invocation compile，START 生成固定 tasks，通过 `Send` fanout 到一个 worker node，再由 `join_review` 生成 canonical report。禁止 retry、proposal 和任何 mutation Tool。

- [ ] **Step 4: 运行 GREEN**

  Run: Task 2 定向 pytest 命令及 Task 1 pytest。

- [ ] **Step 5: 提交生产代码**

  Commit only: `src/assistant_agent/coding/review.py` and any required narrow `workspace.py` change.

  Message: `feat: add read only coding review graph`

---

### Task 3: 父图接入、checkpoint binding 与原生 HITL

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/context.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Create: `tests/tdd/ai-coding-code-review/test_review_lifecycle.py`
- Create: `tests/tdd/ai-coding-code-review/test_review_topology.py`

**Interfaces:**
- Produces parent nodes: `prepare_review_snapshot`, `run_code_review`, `coding_review_decision`
- Produces state fields: review generation/bindings/tasks/results/report/decision and transient review context
- Consumes: `create_coding_review_graph`, `CodingReviewInput`, `CodingReviewReport`
- Preserves: existing validation repair, create_commit, prepare_merge and merge approval semantics.

- [ ] **Step 1: 写父图生命周期 RED 测试**

  覆盖默认 disabled passthrough；enabled validation success -> snapshot -> review -> interrupt；validation failure 不 review；integration disabled 仍 review；approve 后才 commit/terminal；reject 不 commit；公开 topology 显示稳定顺序。

- [ ] **Step 2: 写 resume binding RED 测试**

  覆盖 workspace/generation/base/snapshot/tree/diff/validation/report 任一漂移、过期 interrupt、重复 resume、START overwrite reset、terminal cleanup，以及普通非 coding run 不受影响。

- [ ] **Step 3: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/tdd/ai-coding-code-review/test_review_lifecycle.py tests/tdd/ai-coding-code-review/test_review_topology.py -q`

  Expected: review parent nodes/state/routes 尚不存在。

- [ ] **Step 4: 实现父图最小接入**

  在成功 `run_validation` 后按静态 flag 分流。prepare 节点创建 final snapshot 并 checkpoint binding；wrapper 只向 subgraph 投影 `CodingReviewInput`；decision 节点使用原生 `interrupt`，resume 前走完整 `_resolve_workspace` 与 digest 校验。approve 路由既有 create_commit/terminal，reject 进入既有 rejected terminal。所有 conditional edge 提供稳定 destinations/path_map 以公开 topology。

- [ ] **Step 5: 实现原子状态清理**

  新 generation、patch/diff 或 validation evidence 变化时清空旧 report/approval；terminal 清理临时 task/context 并保留最终 canonical report 和 decision audit fields。

- [ ] **Step 6: 运行 GREEN**

  Run: Task 3 定向 pytest 命令，以及 Task 1/2 全部 Stage 5C TDD。

- [ ] **Step 7: 提交生产代码**

  Commit only: `state.py`, `context.py`, `coding_graph.py`.

  Message: `feat: gate coding integration on review`

---

### Task 4: Replay、错误分类与资源边界加固

**Files:**
- Modify: `src/assistant_agent/coding/review.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Modify only if a demonstrated snapshot defect requires it: `src/assistant_agent/agent_server/workspace.py`
- Create: `tests/tdd/ai-coding-code-review/test_review_hardening.py`

**Interfaces:**
- Hardens: Task 1-3 的公开 contracts，不新增 mutation capability。
- Preserves: permission/cancel/GraphBubbleUp 原异常；稳定能力失败映射为 unavailable。

- [ ] **Step 1: 写 hardening RED 测试**

  覆盖 checkpoint replay 不重复 Provider 调用、内容相同 snapshot 幂等、输出精确大小边界、恶意 schema、duplicate/unknown inventory、bad digest、stale TTL、workspace replacement、cancel/permission/GraphBubbleUp 传播、snapshot 已删除但 completed report 可继续 resume、未完成 read 必须 fail closed。

- [ ] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/tdd/ai-coding-code-review/test_review_hardening.py -q`

  Expected: 每个新增测试先因可复现的缺口失败。

- [ ] **Step 3: 逐个实施最窄修复**

  为每个 RED 只修改拥有该行为的组件；不得扩大 reaper、workspace lifecycle、Provider 或 mutation scope。节点副作用前必须有独立 checkpoint，所有 canonical digest 与错误映射保持 deterministic。

- [ ] **Step 4: 运行 GREEN 与组合回归**

  Run: Stage 5C 全部 TDD；Stage 5B `tests/tdd/ai-coding-parallel-analysis/`；coding/workspace/patch 相关 core。

- [ ] **Step 5: 提交生产修复**

  Commit only demonstrated production fixes.

  Message: `fix: harden coding review lifecycle`

---

### Task 5: Authority、core invariant 与最终验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify if ownership/process text changes: `docs/agent-server-architecture.md`
- Modify if Tool profile ownership changes: `docs/tool-calling-architecture.md`
- Modify: `tests/core/INVARIANTS.md`
- Modify only existing LOOP-001 test if its registered protection requires topology update: corresponding file under `tests/core/`

**Interfaces:**
- Documents: Stage 5C current runtime truth, read-only Tool boundary, snapshot/process ownership and HITL semantics.
- Preserves: `docs/authority.toml` routing unless a new authority file is introduced; this plan introduces none.

- [ ] **Step 1: 同步 owner authority 与 LOOP-001**

  记录 validation -> final review snapshot -> parallel read-only review -> user decision -> commit/integration；明确 default-off、digest binding、unavailable 语义、integration-disabled 行为、snapshot-only reaper 和无自动修复。

- [ ] **Step 2: 运行 authority validator**

  Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`

  Expected: `valid=true`, `errors=[]`。

- [ ] **Step 3: 运行最终验证**

  Run Stage 5C 全 TDD、Stage 5B 全 TDD、`tests/core`。全部命令显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`。

- [ ] **Step 4: 验证现有 8089 hot reload**

  等待唯一 dev server reload，作为客户端请求 `http://127.0.0.1:8089/ok`；不得启动第二套 server。

- [ ] **Step 5: 提交 authority 与 invariant 更新**

  Commit only current authority/core invariant files; do not commit `tests/tdd/ai-coding-code-review/`.

  Message: `docs: document coding review gate`

- [ ] **Step 6: 分支收口**

  确认 feature 分支仅保留任务相关提交和未提交临时 TDD，报告所有验证命令、结果、未调用真实 Provider、临时 TDD 可删除性以及合并选项。

