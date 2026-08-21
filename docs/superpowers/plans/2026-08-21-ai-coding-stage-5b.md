# AI Coding Stage 5B 只读并行分析实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在唯一 `AssistantCodingGraph` 的首次 draft 前增加基于原生 `Send` 的三个只读并行分析任务，并把同一不可变 snapshot 上的有界结构化 evidence 交给唯一 patch proposal 节点。

**Architecture:** repository 静态配置显式启用后，确定性节点创建只读 snapshot 和三个固定 task，通过 LangGraph `Send` 在同一 super-step 派发一个共享 read-only `create_agent`。worker 只返回严格 `CodingAnalysisResult`，reducer 按 task ID 去重，join 确定性校验、排序和裁剪；现有顺序 mutation lane、repair、HITL、validation 和 integration 不变。

**Tech Stack:** Python 3.11、Pydantic v2、LangChain `create_agent`、LangGraph `StateGraph` / `Send` / reducer / `RetryPolicy`、Git 临时 index/tree、pytest。

**Spec:** `docs/superpowers/specs/2026-08-21-ai-coding-stage-5b-parallel-analysis-design.md`

## Global Constraints

- 生产 Assistant 仍只有一个 `AssistantRootGraph` 和一个顺序 mutation lane；不得创建第二套 Runtime、新 run、后台任务或独立可写 Graph。
- 并行 task 固定为 `structure_context`、`change_test_impact`、`safety_governance`，最多三个，不从用户文本或模型输出动态增加。
- 分析 Tool inventory 只含 snapshot-bound list/search/read/status/diff；不得包含 proposal、command、network、credential、artifact 或 integration Tool。
- snapshot 必须内容寻址、身份/thread/workspace 绑定、只读且不修改真实 worktree/index；宿主路径、文件句柄、Git process 和 backend client 不进入 checkpoint。
- 分析 transcript 与临时 task/context 不写入主 `messages`；checkpoint 只保存有界 JSON-safe contract。
- 单个普通 advisory 分析失败可降级；cancel、interrupt、身份、权限、snapshot 隔离和程序 contract 错误不得被吞掉。
- `parallel_analysis_enabled` 默认 `false`；关闭时保持 Stage 5A 行为。
- Stage 5B 只在首次 draft 前执行，repair、formatter respond 和 approval resume 不重复执行。
- feature RED/GREEN 只放 `tests/tdd/ai-coding-parallel-analysis/`，保持未提交且可由用户手动整目录删除。
- 设计 spec、实施 plan 和临时 TDD 不提交；每个 task commit 只包含本 task 生产代码或当前 authority/core 变更。
- 全部 pytest 强制 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、offline，不调用真实 Provider 或网络。
- Core invariant: `LOOP-001` changed because coding graph gains a stable read-only parallel super-step before its sequential mutation lane.

---

### Task 1: 分析 contract、固定 task 与确定性汇聚

**Files:**
- Create: `src/assistant_agent/coding/analysis.py`
- Modify: `src/assistant_agent/coding/models.py`
- Test: `tests/tdd/ai-coding-parallel-analysis/test_analysis_contracts.py`

**Interfaces:**
- Consumes: 现有严格 frozen Pydantic model 风格与 SHA-256 digest 约定。
- Produces: `CodingAnalysisSnapshot`、`CodingAnalysisTask`、`CodingAnalysisFinding`、`CodingAnalysisResult`；`build_analysis_tasks()`、`normalize_analysis_result()`、`merge_analysis_results()`、`join_analysis_results()`、`render_analysis_context()`。

- [ ] **Step 1: 编写 contract RED 测试**

覆盖严格 schema、tuple 冻结、固定三 task、未知 task/tool 拒绝、finding/result 数量与长度上限、模型自报 digest 不可信、task ID reducer 去重和固定顺序。

```python
def test_build_analysis_tasks_is_fixed_and_read_only() -> None:
    tasks = build_analysis_tasks()
    assert [task.task_id for task in tasks] == [
        "structure_context",
        "change_test_impact",
        "safety_governance",
    ]
    assert all("coding_propose_patch" not in task.allowed_tool_names for task in tasks)

def test_merge_analysis_results_replaces_by_stable_task_id() -> None:
    merged = merge_analysis_results([old_result], [new_result])
    assert merged == [new_result]
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis/test_analysis_contracts.py
```

Expected: FAIL，因为 analysis models/helpers 尚不存在。

- [ ] **Step 3: 实现严格 model 与 helper**

`models.py` 增加四个 frozen/strict/extra-forbid model。`analysis.py` 固定三个 task 和 read-only Tool 名；规范化 adapter 自行计算 `finding_id` 与 `output_digest`，不接受 raw model 自报 digest。

```python
ANALYSIS_TASK_IDS = (
    "structure_context",
    "change_test_impact",
    "safety_governance",
)
MAX_FINDINGS_PER_TASK = 12
MAX_TASK_CONTEXT_CHARS = 6_000
MAX_ANALYSIS_CONTEXT_CHARS = 24_000

def merge_analysis_results(current, update) -> list[CodingAnalysisResult]:
    by_id = {item.task_id: item for item in current or ()}
    by_id.update({item.task_id: item for item in update or ()})
    return [by_id[item] for item in ANALYSIS_TASK_IDS if item in by_id]
```

`join_analysis_results()` 必须校验 snapshot ref/tree digest，按固定 task/finding ID 排序、按 path/category/evidence digest 去重，并返回 `completed|partial|unavailable` 与结构化结果。`render_analysis_context()` 只渲染完整 finding，稳定截断，不输出半条 JSON。

- [ ] **Step 4: 运行 GREEN**

运行 Step 2 同一命令。Expected: PASS。

- [ ] **Step 5: 提交生产代码**

```bash
git add src/assistant_agent/coding/analysis.py src/assistant_agent/coding/models.py
git commit -m "feat: define coding parallel analysis contracts"
```

---

### Task 2: 不可变 snapshot backend 与 snapshot-bound Tool

**Files:**
- Modify: `src/assistant_agent/coding/config.py`
- Modify: `src/assistant_agent/coding/workspace.py`
- Modify: `src/assistant_agent/coding/tools.py`
- Test: `tests/tdd/ai-coding-parallel-analysis/test_analysis_snapshot.py`

**Interfaces:**
- Consumes: Task 1 `CodingAnalysisSnapshot`；现有 `CodingWorkspaceService`、path policy 和 read result contracts。
- Produces: repository `parallel_analysis_enabled: bool = False`；`create_analysis_snapshot()`、`resolve_analysis_snapshot()`、`release_analysis_snapshot()`；`build_coding_analysis_tools()`。

- [ ] **Step 1: 编写 snapshot 与 Tool RED 测试**

覆盖默认关闭、当前累计 tracked/untracked 文本被捕获、真实 index/worktree 不变、创建后 worktree 漂移不改变 snapshot、身份/thread/workspace 绑定、TTL、protected path/symlink/size/UTF-8 policy 复用，以及 Tool inventory 精确为五个 read Tool。

```python
def test_snapshot_is_immutable_and_does_not_mutate_real_index(workspace_service, workspace):
    before_status = workspace_service.status(workspace)
    snapshot = workspace_service.create_analysis_snapshot(workspace, identity="u", thread_id="t")
    (workspace.root / "src/app.py").write_text("changed later\n")
    assert workspace_service.read_analysis_snapshot(snapshot, "src/app.py", 1, 20).content == "frozen\n"
    assert workspace_service.status(workspace) != before_status

def test_analysis_tools_are_exactly_read_only():
    tools = build_coding_analysis_tools(service)
    assert {tool.name for tool in tools} == {
        "coding_repo_list", "coding_repo_search", "coding_repo_read",
        "coding_repo_status", "coding_repo_diff",
    }
    assert all(tool.metadata["effect"] == "read" for tool in tools)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis/test_analysis_snapshot.py
```

Expected: FAIL，因为 snapshot/config/tool API 尚不存在。

- [ ] **Step 3: 实现内容寻址 snapshot**

在 workspace exclusive lock 内使用临时 Git index：`read-tree HEAD -> add -A -> write-tree`，计算 tree/diff digest，随后将 tree 物化到 workspace management root 下的只读 snapshot 目录。必须使用临时 index 环境变量，不修改真实 index；失败清理临时 index/目录并返回稳定 `coding_analysis_snapshot_*` 错误。

metadata 保存 identity/thread/workspace digest、tree digest、created/expires time；Graph contract 不保存物理 path。resolve 每次重新校验 identity、thread、workspace、TTL 和 metadata。

- [ ] **Step 4: 实现 snapshot-bound read Tool factory**

复用现有 list/search/read/status/diff 的有界 result 与 path policy，但 root 固定解析为 snapshot。Tool 通过 `ToolRuntime` state 读取 opaque `analysis_snapshot`，不接受模型提供 snapshot ref 或宿主路径。不得复用含 `coding_propose_patch` 的普通 inventory。

- [ ] **Step 5: 运行 GREEN 与历史 workspace 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis/test_analysis_snapshot.py \
  tests/tdd/ai-coding-workspace tests/tdd/ai-coding-patch
```

Expected: PASS。

- [ ] **Step 6: 提交生产代码**

```bash
git add src/assistant_agent/coding/config.py src/assistant_agent/coding/workspace.py \
  src/assistant_agent/coding/tools.py
git commit -m "feat: add immutable coding analysis snapshots"
```

---

### Task 3: 原生 Send 并行 Graph 与 primary context

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Test: `tests/tdd/ai-coding-parallel-analysis/test_parallel_analysis_graph.py`

**Interfaces:**
- Consumes: Tasks 1–2 contracts/helpers/snapshot tools；现有 `inspect_and_draft`、repair budget gate 和顺序 mutation lane。
- Produces: `prepare_analysis`、`analyze_workspace`、`join_analysis` nodes；`route_analysis_workers() -> list[Send] | str`；首次 primary 临时 analysis context。

- [ ] **Step 1: 编写 topology、并发与 context RED 测试**

覆盖默认关闭直达 inspect；启用后固定三 `Send`；barrier fake 证明并行；同一 snapshot；分析 agent 看不到 propose；worker messages 不进入主 messages；join 后 primary 收到临时 context 且 primary transcript 正常持久；只有 primary 能产生 proposal。

```python
async def test_enabled_analysis_workers_run_concurrently(graph, barrier_agent):
    result = await graph.ainvoke(coding_input(parallel_analysis_enabled=True))
    assert barrier_agent.max_concurrency == 3
    assert {item.task_id for item in result["analysis_results"]} == set(ANALYSIS_TASK_IDS)

def test_default_disabled_graph_skips_analysis(...):
    assert invoked_nodes == ["resolve_workspace", "inspect_and_draft"]
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis/test_parallel_analysis_graph.py
```

Expected: FAIL，因为 Graph nodes/state 尚不存在。

- [ ] **Step 3: 扩展 state 与构造 read-only agent**

`CodingState` 增加 optional snapshot/tasks/results/status channels，`analysis_results` 使用 Task 1 reducer。`build_coding_graph()` 接受可注入 `analysis_agent`，默认用 `build_coding_analysis_tools()` 与 `create_agent(response_format=...)` 创建共享 compiled read-only agent；每个 worker 使用局部 messages projection，不回写 transcript。

- [ ] **Step 4: 实现 prepare / Send worker / join**

`resolve_workspace` 后 router 仅根据 repository 静态配置和 state status 决定是否进入 prepare。`route_analysis_workers` 为三个 task 构造最小 worker state 并返回 `Send`；worker 规范化 result；join 调用确定性 helper，并释放 active snapshot lease。

primary 首次调用将 `render_analysis_context()` 结果作为临时 `HumanMessage` 追加到局部 call state。repair active 时只使用现有 repair context，不重复 analysis context；主 messages update 仍只追加 primary 新消息。

- [ ] **Step 5: 运行 GREEN 与 Stage 5A TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis/test_parallel_analysis_graph.py \
  tests/tdd/ai-coding-repair-loop
```

Expected: PASS。

- [ ] **Step 6: 提交生产代码**

```bash
git add src/assistant_agent/native_agent/state.py src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: add native coding analysis fanout"
```

---

### Task 4: checkpoint、失败降级与生命周期闭环

**Files:**
- Modify: `src/assistant_agent/coding/analysis.py`
- Modify: `src/assistant_agent/coding/workspace.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Test: `tests/tdd/ai-coding-parallel-analysis/test_parallel_analysis_lifecycle.py`

**Interfaces:**
- Consumes: Task 3 完整 fan-out Graph。
- Produces: 稳定 partial/unavailable/stale/error 行为；checkpoint resume 去重；repair/formatter/approval resume 跳过；snapshot TTL/release 边界。

- [ ] **Step 1: 编写 lifecycle RED 测试**

覆盖单 worker 普通临时失败经 `RetryPolicy` 后 partial、全部普通失败 unavailable、schema invalid、安全/身份错误 fail closed、cancel/`GraphBubbleUp` 传播、乱序结果稳定、checkpoint 重放按 task ID 去重、已完成分析从 START 恢复跳过、repair active 先走 repair budget、formatter/respond 和 approval resume 不重跑、snapshot expired 不静默复用。

```python
async def test_all_advisory_failures_continue_with_unavailable(...):
    result = await graph.ainvoke(input_state)
    assert result["analysis_status"] == "unavailable"
    assert primary_agent.called

async def test_identity_error_fails_closed_instead_of_advisory_fallback(...):
    with pytest.raises(CodingWorkspaceError, match="coding_analysis_identity_mismatch"):
        await graph.ainvoke(input_state)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis/test_parallel_analysis_lifecycle.py
```

Expected: FAIL，暴露 Task 3 尚未补齐的 lifecycle 分支。

- [ ] **Step 3: 实现确定性失败分类与 resume router**

普通 timeout/connection/transient Provider HTTP 在 worker node 的 `RetryPolicy` 耗尽后转换为不含异常正文的 failed result。identity/permission/snapshot contract/program error 与 `GraphBubbleUp` 不降级。router 优先处理 terminal、active repair 和已完成 analysis；pending snapshot 只能恢复同一 digest。

- [ ] **Step 4: 实现 release/TTL 与状态清理**

join 后释放 active lease但保留受管 snapshot 到 TTL；cleanup 失败记录稳定脱敏状态并由 owner reaper 处理。进入 primary 后不保留 worker 临时授权或 transcript。新 coding 周期才能原子清理旧 analysis state 并创建新 snapshot。

- [ ] **Step 5: 运行 Stage 5B 全部 TDD 与历史 coding 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis \
  tests/tdd/ai-coding-graph tests/tdd/ai-coding-validation \
  tests/tdd/ai-coding-integration tests/tdd/ai-coding-repair-loop
```

Expected: PASS。

- [ ] **Step 6: 提交生产代码**

```bash
git add src/assistant_agent/coding/analysis.py src/assistant_agent/coding/workspace.py \
  src/assistant_agent/native_agent/coding_graph.py src/assistant_agent/native_agent/state.py
git commit -m "fix: harden coding analysis lifecycle"
```

---

### Task 5: Authority、core invariant 与阶段验收

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/authority.toml`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`

**Interfaces:**
- Consumes: 完整 Stage 5B 实现及测试事实。
- Produces: 当前根级 authority、manifest source routing 与 `LOOP-001` 对只读并行 super-step 的稳定登记。

- [ ] **Step 1: 更新 `LOOP-001` 最小结构化断言**

在既有 `tests/core/integration/test_runtime_lifecycle.py` 中扩展现有 LOOP-001 测试，只断言生产 coding graph 具有分析 fan-out/join 后进入唯一顺序 mutation lane的稳定结构；不导入 feature implementation、不断言 prompt 文案、私有调用次数或固定 task 业务内容。

- [ ] **Step 2: 同步 runtime 与 Agent Server authority**

记录：repository 默认关闭配置；process owner 的 snapshot 生命周期；Graph checkpoint 仅保存 opaque snapshot 与有界 result；原生 `Send` 只读并行；partial/unavailable 降级；主 messages 不保存 worker transcript；repair/approval resume 不重跑；所有 mutation 仍顺序治理。

- [ ] **Step 3: 更新 authority manifest**

把 `src/assistant_agent/coding/analysis.py` 登记到正确 owner domain，并把 Stage 5B 临时验证命令加入相应 verification 列表。只修改当前 owner，不机械扩散其他 authority。

- [ ] **Step 4: 运行阶段完整验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-parallel-analysis

MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: Stage 5B TDD、默认 core 全部 PASS；authority JSON 为 `valid: true` 且无 errors。

- [ ] **Step 5: 运行历史 coding 回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONDONTWRITEBYTECODE=1 \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-workspace tests/tdd/ai-coding-patch \
  tests/tdd/ai-coding-graph tests/tdd/ai-coding-validation \
  tests/tdd/ai-coding-integration tests/tdd/ai-coding-sandbox \
  tests/tdd/ai-coding-dependencies tests/tdd/ai-coding-credential-broker \
  tests/tdd/ai-coding-artifact-governance tests/tdd/ai-coding-repair-loop
```

Expected: PASS；若过期历史断言与当前已登记 invariant 冲突，记录准确差异，不通过修改生产代码恢复旧行为。

- [ ] **Step 6: 提交 authority/core 变更**

```bash
git add docs/runtime-event-stream-architecture.md docs/agent-server-architecture.md \
  docs/authority.toml tests/core/INVARIANTS.md \
  tests/core/integration/test_runtime_lifecycle.py
git commit -m "docs: document coding parallel analysis"
```

## Stage Completion

完成全部 task、逐 task review、整分支 final review 和必要修复后：

```text
完成：AI Coding Stage 5B 只读并行分析。
Core invariant: LOOP-001 changed to include the native read-only analysis super-step before the sequential coding mutation lane.
Tests: tests/tdd/ai-coding-parallel-analysis is temporary RED/GREEN and may be deleted manually by the user.
```

本地合并到 `cqy` 后，在主工作区重跑 Stage 5B TDD、默认 core、authority validator，并仅作为客户端验证现有
`http://127.0.0.1:8089/ok` 热重载；不得启动第二个 Agent Server。
