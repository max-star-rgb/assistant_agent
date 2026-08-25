# AI Coding Stage 5D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Stage 5C review decision 增加用户驱动、最多两轮、完整 checkpoint/digest 绑定的受控修复闭环。

**Architecture:** `respond` 先冻结 repair context 并在独立 checkpoint node 消费预算，再回到既有 inspect/draft mutation lane；任何新 patch 都重新经过 proposal approval、validation、review 和 decision。Review Graph 保持只读，父图独占全部 mutation 和 lease lifecycle。

**Tech Stack:** Python 3.12、Pydantic v2、LangGraph StateGraph/interrupt/checkpoint、pytest、现有 coding snapshot/validation/integration services。

**Spec:** `docs/superpowers/specs/2026-08-24-ai-coding-stage-5d-review-repair-design.md`

## Global Constraints

- `respond` 仅对 current `findings` report 合法；clean/unavailable 只能 approve/reject。
- `MAX_CODING_REVIEW_REPAIR_ATTEMPTS = 2`，必须在独立 checkpoint node 先消费预算。
- repair 后必须重新 patch approval、validation、review、decision；禁止任何 shortcut。
- reviewer 始终只读，无 shell/network/provider search/mutation authority。
- digest/schema/workspace/identity/path policy 错误 fail closed，cancel/GraphBubbleUp 原样传播。
- pytest 只用 mock/offline；临时 TDD 不提交；不扩大 snapshot-only reaper 或 worktree/admin owner。

---

### Task 1: Review repair contracts、预算与审计 history

**Files:**
- Modify: `src/assistant_agent/coding/models.py`
- Create: `src/assistant_agent/coding/review_repair.py`
- Create: `tests/tdd/ai-coding-review-repair/test_review_repair_contracts.py`

**Interfaces:**
- Produces: `MAX_CODING_REVIEW_REPAIR_ATTEMPTS`
- Produces: `CodingReviewRepairContext`, `CodingReviewRepairAttempt`
- Produces: `normalize_review_response(...)`, `build_review_repair_context(...)`, `validate_review_repair_history(...)`

- [ ] 写固定 findings summary、response 精确长度/UTF-8/JSON、attempt 0..2、history 连续性和 canonical digest RED。
- [ ] 运行定向 pytest，确认因契约缺失失败。
- [ ] 实现 frozen strict Pydantic models、canonical response/digest 和最多两项 history 校验。
- [ ] 运行 GREEN 并提交生产文件：`feat: define coding review repair contracts`。

---

### Task 2: Respond decision、预算 checkpoint 与一次性 inspect context

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Create: `tests/tdd/ai-coding-review-repair/test_review_repair_lifecycle.py`
- Create: `tests/tdd/ai-coding-review-repair/test_review_repair_topology.py`

**Interfaces:**
- Produces nodes: `consume_review_repair_budget`, existing `inspect_and_draft` repair projection
- Consumes: Task 1 context/history helpers

- [ ] 写 findings-only respond、approve/reject unchanged、第1/2次先消费后 inspect、第3次 exhausted 不调用、public topology RED。
- [ ] 写 repair context 只投影首次 inspect、formatter/validation repair 不重复消费的 RED。
- [ ] 运行 RED。
- [ ] 扩展 decision parser 和 conditional routes；独立 checkpoint node 原子消费预算；inspect prompt/state 投影 bounded context。
- [ ] 运行 GREEN 与 Stage 5C covering tests，提交：`feat: add review repair checkpoint loop`。

---

### Task 3: 原子失效、全链重审与 snapshot lease lifecycle

**Files:**
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Modify if required by existing release API: `src/assistant_agent/coding/workspace.py`
- Create: `tests/tdd/ai-coding-review-repair/test_review_repair_reset.py`
- Create: `tests/tdd/ai-coding-review-repair/test_review_repair_leases.py`

**Interfaces:**
- Produces: atomic respond reset update and deterministic old validation/review snapshot release
- Preserves: Stage 5C validation-snapshot same-bytes and integration tree binding

- [ ] 写旧 patch/approval/validation/review/integration channel 全清、identity/generation/request/count/history 保留 RED。
- [ ] 写 old lease success/failure/idempotent release RED。
- [ ] 写 respond 后必须新 proposal approval -> validation -> final review，旧 approval/digest 不能复用 RED。
- [ ] 实现最小 reset/release/full-chain routing，释放异常只记录 reaper status。
- [ ] 运行 GREEN、Stage 5C/5B/workspace/patch组合回归，提交：`fix: bind review repair lifecycle`。

---

### Task 4: Resume、replay 与恶意 checkpoint hardening

**Files:**
- Modify: `src/assistant_agent/coding/review_repair.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Create: `tests/tdd/ai-coding-review-repair/test_review_repair_hardening.py`

**Interfaces:**
- Hardens Task 1-3 contracts without new capability

- [ ] 写 stale/replayed respond、digest/schema/workspace drift、orphaned/duplicate history、count overflow、互斥 approval channels RED。
- [ ] 写 permission/identity/snapshot/path/digest fail closed 与 cancel/GraphBubbleUp 传播 RED。
- [ ] 运行 RED；逐项最窄修复，不增加 retry 或 reaper scope。
- [ ] 运行 GREEN 与 Stage 5D/5C/5B组合回归，提交：`fix: harden coding review repair resume`。

---

### Task 5: Authority、LOOP-001 与最终验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify if current Tool boundary requires clarification: `docs/tool-calling-architecture.md`
- Modify if Agent Server interrupt/resume ownership changes: `docs/agent-server-architecture.md`
- Modify: `tests/core/INVARIANTS.md`
- Modify only existing registered LOOP-001 core test when required: `tests/core/integration/test_runtime_lifecycle.py`

**Interfaces:**
- Documents current review-repair topology, budget, bindings, lease lifecycle and default behavior

- [ ] 同步 owner authority 与 LOOP-001，只声明生产与永久测试真实保护的行为。
- [ ] 运行 Stage 5D、Stage 5C、Stage 5B、workspace、patch、core、authority validator 和 `git diff --check`。
- [ ] 只探测现有 8089 健康，不启动第二个 server、不声称加载 worktree。
- [ ] 提交 authority/core：`docs: document coding review repair loop`。
- [ ] 执行任务级审查、全分支最终审查和至多一个最终修复波次；报告 rulings、临时 TDD 和真实 Provider 使用情况。

