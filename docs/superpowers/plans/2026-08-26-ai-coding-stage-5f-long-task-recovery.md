# AI Coding Stage 5F Long Task Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为生产 `AssistantCodingGraph` 增加最多两次、checkpointed、可审计且能确定性终止 no-progress 的 primary inspect 长任务恢复。

**Architecture:** 保留单次 inspect 的既有 Tool/Model 数值预算，把 primary inspect middleware 改为官方 graceful termination，再从返回的标准 messages 和 middleware counter 提取不含源码的 canonical progress。CodingGraph 通过两个新节点消费恢复预算和临时 context，始终复用同一 thread、run、workspace 与顺序 mutation lane。

**Tech Stack:** Python 3.12、Pydantic v2、LangChain `create_agent` middleware、LangGraph `StateGraph`/checkpoint、pytest。

**Spec:** `docs/superpowers/specs/2026-08-26-ai-coding-stage-5f-long-task-recovery-design.md`

## Global Constraints

- 开发和 pytest 固定 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- primary inspect 每个 epoch 保留现有 `model_call_limit` 和 `tool_call_limit` 数值，最多两个 recovery epoch。
- 只恢复 canonical Tool/Model budget exhaustion；Provider、permission、identity、cancel、sandbox、workspace 和未知错误继续 fail closed。
- checkpoint 不保存源码、原始 Tool result、完整 inspect transcript、prompt 或 Provider response。
- 不修改 Stage 5E case、grader、真实 operator 门禁或 error projection。
- 不建立第二套 Runtime、run retry、workspace 或 mutation lane。
- 不启用 Provider-native code execution、远程 push 或 PR。

---

### Task 1: Inspect recovery contract 与 state channel

**Files:**
- Modify: `src/assistant_agent/coding/models.py:1395`
- Create: `src/assistant_agent/coding/inspect_recovery.py`
- Modify: `src/assistant_agent/native_agent/state.py:157`
- Create: `tests/tdd/ai-coding-long-task-recovery/test_contracts.py`

**Interfaces:**
- Produces: `CodingInspectCallEvidence`, `CodingInspectProgress`, `CodingInspectRecoveryAttempt`。
- Produces: `canonical_inspect_progress_digest(progress) -> str`。
- Produces: `validate_inspect_recovery_history(values) -> tuple[CodingInspectRecoveryAttempt, ...]`。
- Extends: `CodingTerminalResult.inspect_recovery_status` 与 `inspect_recovery_history`。
- Extends: `CodingState` 的 `inspect_epoch`、`inspect_recovery_status`、`inspect_progress`、`inspect_recovery_history`、`inspect_recovery_context_consumed`。

- [ ] **Step 1: 写 contract RED**

在 `test_contracts.py` 用严格 Pydantic 输入覆盖：合法 epoch 1..3、排序去重后的 evidence、extra field、非法 hex、绝对路径、`..`、超过 32 paths、超过 64 calls、history epoch 跳跃、重复 progress digest 和非法 outcome transition。

```python
def test_progress_contract_rejects_host_and_oversize_paths() -> None:
    valid = {
        "tool_name": "coding_read_file",
        "arguments_digest": "a" * 64,
        "result_digest": "b" * 64,
        "relative_paths": ("src/calc.py",),
    }
    assert CodingInspectCallEvidence(**valid).relative_paths == ("src/calc.py",)
    for path in ("/tmp/secret", "../escape.py", "src/../../escape.py"):
        with pytest.raises(ValidationError):
            CodingInspectCallEvidence(**{**valid, "relative_paths": (path,)})
```

- [ ] **Step 2: 运行 contract RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery/test_contracts.py
```

Expected: collection/import fails because Stage 5F contracts do not exist.

- [ ] **Step 3: 实现严格 contract 与 canonical history validator**

模型使用 `ConfigDict(extra="forbid", frozen=True, strict=True)`；`progress_digest` 的 canonical payload 不包含 epoch、时间、thread/run ID。history validator 强制 epoch 连续、最多三项、只有最新项可为 `pending|retrying`。

```python
class CodingInspectProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    epoch: int = Field(ge=1, le=3)
    reason: Literal["tool_budget_exhausted", "model_budget_exhausted"]
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    workspace_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    calls: tuple[CodingInspectCallEvidence, ...] = Field(max_length=64)
    progress_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
```

- [ ] **Step 4: 将 contract 加入 `CodingState` 和 terminal audit**

新 cycle 默认 `inspect_epoch=1`、history 空、status/progress 为 `None`、context consumed 为 `False`。不要为顺序 history 添加 append reducer；每个节点返回完整 canonical tuple。

- [ ] **Step 5: 运行 contract GREEN**

Run Task 1 RED 命令。Expected: PASS。

- [ ] **Step 6: 提交 Task 1 生产文件**

```bash
git add src/assistant_agent/coding/models.py src/assistant_agent/coding/inspect_recovery.py src/assistant_agent/native_agent/state.py
git commit -m "feat: add coding inspect recovery contracts"
```

临时 TDD 不加入提交。

---

### Task 2: Graceful budget termination 与 progress extractor

**Files:**
- Modify: `src/assistant_agent/native_agent/coding_graph.py:320`
- Modify: `src/assistant_agent/coding/inspect_recovery.py`
- Create: `tests/tdd/ai-coding-long-task-recovery/test_budget_termination.py`

**Interfaces:**
- Consumes: Task 1 contracts。
- Produces: `extract_inspect_progress(result, *, epoch, base_commit, workspace_diff_digest, read_tool_names, model_call_limit, tool_call_limit) -> CodingInspectProgress | None`。
- Produces: `render_inspect_recovery_context(history) -> str`，仅包含 tool 名、canonical 相对路径和固定恢复规则。

- [ ] **Step 1: 写 middleware/extractor RED**

使用 scripted chat model 与两个标准 read `BaseTool`，让模型提交 13 个 Tool call。断言前 12 个执行、第 13 个成为
`ToolMessage(status="error")`、invocation 正常返回；再构造 `run_model_call_count == model_call_limit` 且无 proposal 的结果，断言 reason 为 `model_budget_exhausted`。

```python
progress = extract_inspect_progress(
    result,
    epoch=1,
    base_commit=BASE,
    workspace_diff_digest="c" * 64,
    read_tool_names=frozenset({"coding_read_file"}),
    model_call_limit=12,
    tool_call_limit=12,
)
assert progress is not None
assert progress.reason == "tool_budget_exhausted"
assert all(call.tool_name == "coding_read_file" for call in progress.calls)
```

- [ ] **Step 2: 运行 budget RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery/test_budget_termination.py
```

Expected: extractor missing，且现有 `exit_behavior="error"` 抛出 limit exception。

- [ ] **Step 3: 只调整 primary inspect middleware**

```python
middleware = [
    ModelCallLimitMiddleware(run_limit=model_call_limit, exit_behavior="end"),
    ToolCallLimitMiddleware(run_limit=tool_call_limit, exit_behavior="continue"),
]
```

analysis/review composition 保持 `exit_behavior="error"`。不得通过捕获所有 `Exception` 转换预算终态。

- [ ] **Step 4: 实现结构化 budget detection**

Tool exhaustion 只由 `result["run_tool_call_count"]["__all__"] > tool_call_limit` 判断；Model exhaustion只在没有 proposal、没有 Tool exhaustion且 `result["run_model_call_count"] >= model_call_limit` 时成立。禁止匹配官方英文 message content。

evidence 只关联静态 read Tool 的 `AIMessage.tool_calls[]` 与相同 `tool_call_id` 的标准 `ToolMessage`。result digest 只对
ToolMessage 的 canonical model-visible content 计算；artifact 不序列化进 checkpoint。路径仅从 `path` 或 `paths` 字段提取并经过 repository-relative validator。

- [ ] **Step 5: 实现临时 recovery context renderer**

输出最多 32 个相对路径和 12 个 Tool 名；不含源码、digest、Tool result、错误原文或内部 ID。空 evidence 仍可生成固定的
“减少重复读取并形成 proposal”指令，但下一 epoch 无新增 evidence 时由 Task 3 no-progress 终止。

- [ ] **Step 6: 运行 budget GREEN**

Run Task 2 RED 命令。Expected: PASS。

- [ ] **Step 7: 提交 Task 2 生产文件**

```bash
git add src/assistant_agent/native_agent/coding_graph.py src/assistant_agent/coding/inspect_recovery.py
git commit -m "feat: terminate coding inspect budgets gracefully"
```

---

### Task 3: Checkpointed inspect epoch 与 no-progress 路由

**Files:**
- Modify: `src/assistant_agent/native_agent/coding_graph.py:561`
- Create: `tests/tdd/ai-coding-long-task-recovery/test_recovery_graph.py`

**Interfaces:**
- Consumes: `extract_inspect_progress`、`render_inspect_recovery_context` 和 Task 1 state。
- Produces nodes: `evaluate_inspect_progress_node`、`consume_inspect_recovery_context_node`。
- Produces router: `after_inspect(state) -> Literal["validate_proposal", "evaluate_inspect_progress", "summarize"]`。
- Preserves: existing `validate_proposal -> approval -> apply -> validation -> review -> integration` lane。

- [ ] **Step 1: 写 graph RED**

使用 scripted inspect agent 顺序返回：epoch 1 budget result、epoch 2 proposal。断言同一 workspace/ref/base、analysis 只执行一次、
epoch 2 临时 input 含 recovery context、父 `messages` 不含该 context，最终仍进入既有 approval interrupt。

再覆盖两条终止路径：epoch 2 evidence 是 epoch 1 子集时 `coding_inspect_no_progress`；三个 epoch 持续新增 evidence但无 proposal时
`coding_inspect_recovery_exhausted`。

- [ ] **Step 2: 运行 graph RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery/test_recovery_graph.py
```

Expected: new nodes/state transitions missing。

- [ ] **Step 3: 扩展 `inspect_and_draft_node` 的三态返回**

- 合法 proposal：清空 progress/context，将最新 retrying attempt 标为 completed，沿用现有 proposal update。
- canonical budget exhaustion：保存 `inspect_progress` 并设置 status pending，不生成 terminal result。
- 其他失败：沿用现有 terminal/error，不进入 recovery。

重试时只在本次 `call_state` 最新真实用户请求前追加临时 `HumanMessage`；不得更新父 `messages`。

- [ ] **Step 4: 实现 `evaluate_inspect_progress_node`**

canonical call key 为 `(tool_name, arguments_digest, result_digest)`。当前集合是上一 epoch 集合子集、digest 重复或 context consumed
后无新 path 时返回 `coding_inspect_no_progress`。epoch 已为 3 且仍无 proposal时返回
`coding_inspect_recovery_exhausted`。否则追加 retrying attempt、将 epoch 加一并转 context node。

- [ ] **Step 5: 实现 context consumption 与 topology**

注册两个节点，并把现有 `inspect_and_draft -> validate_proposal` 单边改为条件边：

```text
inspect_and_draft -> validate_proposal | evaluate_inspect_progress | summarize
evaluate_inspect_progress -> consume_inspect_recovery_context | summarize
consume_inspect_recovery_context -> inspect_and_draft
```

任何 recovery 节点都不得直达 apply、validation、review、commit 或 merge。

- [ ] **Step 6: 运行 graph GREEN 与既有 Stage 5E graph covering**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery/test_recovery_graph.py tests/tdd/ai-coding-behavior-eval/test_final_review_closure.py
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 3 生产文件**

```bash
git add src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: recover bounded coding inspect epochs"
```

---

### Task 4: Resume、binding 与 cleanup hardening

**Files:**
- Modify: `src/assistant_agent/coding/inspect_recovery.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py:2850`
- Create: `tests/tdd/ai-coding-long-task-recovery/test_resume_cleanup.py`

**Interfaces:**
- Produces: `validate_inspect_recovery_checkpoint(state, *, base_commit, workspace_diff_digest) -> tuple[...]`。
- Extends: new-cycle reset、terminal projection、snapshot release 和 reload mismatch fail-closed。

- [ ] **Step 1: 写 resume/cleanup RED**

覆盖 pending epoch checkpoint resume、completed analysis 不重跑、同一 workspace 复用、新 cycle 全清、proposal completed audit、terminal
snapshot release、cancel cleanup、base/workspace/progress/history drift、extra field 和 impossible status/context combinations。

```python
with pytest.raises(ValueError, match="coding_inspect_recovery_binding_mismatch"):
    validate_inspect_recovery_checkpoint(
        drifted_state,
        base_commit=BASE,
        workspace_diff_digest="d" * 64,
    )
```

- [ ] **Step 2: 运行 hardening RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery/test_resume_cleanup.py
```

Expected: checkpoint validator/reset/cleanup behavior missing。

- [ ] **Step 3: 在所有恢复入口执行 binding validator**

验证 execution attestation、identity/thread/repo/workspace 使用现有 helper；Stage 5F validator只追加 base、workspace diff、epoch、history、
status 和 context-consumed 组合。不要复制 workspace 或 auth policy。

- [ ] **Step 4: 完成 reset、terminal audit 和 cleanup**

新 cycle 原子清空 recovery channels；proposal成功只保留 completed canonical history；no-progress/exhausted terminal把 status/history投影到
`CodingTerminalResult`。复用 `_release_terminal_coding_snapshots`，不得创建第二套 reaper。

- [ ] **Step 5: 运行 hardening GREEN 与全 Stage 5F TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 4 生产文件**

```bash
git add src/assistant_agent/coding/inspect_recovery.py src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: harden coding inspect recovery resume"
```

---

### Task 5: Core topology、authority 与阶段收口

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/authority.toml`
- Optionally modify after owner review: `evals/README.md`

**Interfaces:**
- Registers: `src/assistant_agent/coding/inspect_recovery.py` under `runtime-event-stream` authority。
- Changes: `LOOP-001` only at public topology level。
- Does not change: `RUN-001`、`TOOL-001`、`GATE-001`、`IDENT-001`。

- [ ] **Step 1: 写最小 core topology RED**

扩展既有 `LOOP-001` 测试，只断言：两个 recovery node 存在；inspect条件目标包含 validate/evaluate/summarize；recovery context只回到
inspect；不存在 recovery 到 mutation/review/integration 的 shortcut。不得把 epoch 数、错误文案或私有 helper 加入 core。

- [ ] **Step 2: 运行定向 core RED/GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_runtime_lifecycle.py
```

Expected after topology implementation: PASS；若 RED 先因 invariant 文案/断言缺失而失败，完成最小更新后重跑。

- [ ] **Step 3: 同步 runtime authority 与 manifest**

文档明确 graceful counter、三 epoch、no-progress、checkpoint 无原始 transcript、错误边界和与 validation/review repair 的正交关系。
`docs/authority.toml` 只新增 `src/assistant_agent/coding/inspect_recovery.py` source glob。复核 `evals/README.md`；只有当前 authority
需要声明 Stage 5F 消费 Stage 5E evidence 时才修改，不机械制造 diff。

- [ ] **Step 4: 运行全阶段 covering**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-long-task-recovery
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-behavior-eval
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_runtime_lifecycle.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
```

Expected: all commands exit 0；不启动真实 Provider、不运行真实 Stage 5E baseline。

- [ ] **Step 5: 检查现有 `8089` hot reload**

仅当用户现有 PyCharm dev server 正在 `8089` 监听时，等待 reload 并请求 `/ok`；不得另起并行 Server。若没有 listener，报告未执行，
不把它伪装为失败。

- [ ] **Step 6: 提交 authority/core 生产收口**

```bash
git add docs/runtime-event-stream-architecture.md docs/authority.toml tests/core/INVARIANTS.md tests/core/integration/test_runtime_lifecycle.py
git commit -m "docs: govern coding inspect recovery"
```

设计、计划和 `tests/tdd/ai-coding-long-task-recovery/` 默认保持未提交，除非用户另行要求。

---

## Plan Self-Review

- Spec coverage: graceful budget、progress contract、epoch/no-progress、resume/cleanup、core/authority 和验证均有对应 Task。
- Scope: 不包含 Stage 5E error projection、模型 prompt 调优、远程 Git 或 Provider-native code execution。
- Type consistency: 三个 Pydantic contract、五个 state channel、两个 node 和一个 router 在 Task 1/3 定义并由后续 Task复用。
- Test boundary: 具体恢复行为全部位于临时 TDD；core 只保护 `LOOP-001` 公开拓扑。
