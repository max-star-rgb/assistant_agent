# AI Coding 阶段 3 受控 Commit 与合并实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段 2 验证通过后，为 thread-scoped coding worktree 创建受控 commit，并经独立 HITL 将确定性无冲突结果合并到服务端配置的目标分支。

**Architecture:** 新增受信 `CodingIntegrationService`，模型不可见。service 先在 detached coding worktree 创建固定身份、禁用 hooks/signing 的 commit；再冻结 `source_commit + expected_target_head + result_tree/result_commit`。非 fast-forward 情况使用 `git merge-tree --write-tree` 与 `git commit-tree` 预生成双亲 result commit，最终目标写入统一为 `git merge --ff-only result_commit`。Graph 在 preview 后产生独立 merge interrupt，审批绑定 source、target 与 preview digest；任何漂移、dirty、冲突或 digest 不匹配均 fail closed。

**Tech Stack:** Python 3.12、Pydantic、LangGraph StateGraph/interrupt、固定 Git CLI、pytest。

**Spec:** `docs/superpowers/specs/2026-08-20-ai-coding-agent-design.md`

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；pytest 不访问真实 Provider、网络或付费服务。
- integration 默认关闭；repository 必须显式 `integration_enabled=true` 且配置非空 `verification_sequence`。
- 模型、客户端、messages 和 Runtime Context 都不能提交 target branch、commit message、author、Git argv、merge strategy 或 merge decision facts。
- controlled commit 只发生在 thread-scoped detached worktree；source repository 目标工作区在 merge approval 前不得变化。
- Git hooks、GPG signing、credential prompt、system config 和 editor 全部禁用；不执行任意 shell。
- merge preview 绑定 `source_commit + expected_target_head + result_tree + result_commit + strategy`，其 canonical JSON SHA-256 为 `merge_preview_digest`。
- 目标 path 必须是配置的 source repository、当前 checkout 精确等于 `target_branch`、worktree/index clean，且 HEAD 等于 frozen expected head。
- 冲突、目标 HEAD 漂移、目标 dirty、错误 digest 或审批拒绝时，不更新目标 ref/worktree，不自动重算 preview，不调用模型修复。
- 非 FF preview 使用 `git merge-tree --write-tree` 和 `git commit-tree` 创建 dangling result commit；最终目标写入只允许 `git merge --ff-only result_commit`。
- 阶段 3 不提供 push、PR、remote fetch/pull、远程凭据、部署、依赖安装或冲突修复。
- 新测试放入 `tests/tdd/ai-coding-integration/`，不加入默认 pytest，不提交该临时目录。

---

### Task 1: 定义 Integration 配置与稳定契约

**Files:**
- Modify: `src/assistant_agent/coding/config.py`
- Modify: `src/assistant_agent/coding/models.py`
- Modify: `.env.example`
- Create: `tests/tdd/ai-coding-integration/test_integration_contracts.py`

**Interfaces:**
- Changes: `CodingRepositoryConfig.integration_enabled: bool = False`
- Changes: `CodingRepositoryConfig.commit_author_name` / `commit_author_email`
- Produces: `CodingCommitResult`、`CodingMergePreview`、`CodingMergeApprovalDecision`、`CodingMergeResult`
- Changes: `CodingTerminalResult.status` supports `merged`; adds source/target/result/preview fields

- [ ] **Step 1: 写 RED 配置测试**

覆盖：integration 默认关闭；启用时必须有验证 sequence；target branch 通过 `git check-ref-format --branch`；author name/email 为单行有界值；公开 `AssistantRootInput` 不出现 target/commit/merge 字段。

- [ ] **Step 2: 写 RED 模型测试**

断言 commit/preview/decision/result 均 strict/frozen/extra-forbid；Git object ID 为 40-64 lowercase hex；merge decision 只允许 approve/reject，并要求 approve payload 可携带 source commit、expected target head 与 preview digest。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_integration_contracts.py
```

- [ ] **Step 4: 实现配置与模型**

repository validator 在 `integration_enabled=true` 时要求非空 `verification_sequence`。target branch 使用固定 `git check-ref-format --branch` 校验，不拼入 rev expression；runtime 始终构造 `refs/heads/<target_branch>`。commit author 只来自配置，拒绝 CR/LF/NUL。

- [ ] **Step 5: 运行 GREEN 并提交生产契约**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_integration_contracts.py
git add .env.example src/assistant_agent/coding/config.py src/assistant_agent/coding/models.py
git commit -m "feat: define controlled coding integration contracts"
```

---

### Task 2: 创建验证绑定的受控临时 Commit

**Files:**
- Create: `src/assistant_agent/coding/integration.py`
- Create: `tests/tdd/ai-coding-integration/conftest.py`
- Create: `tests/tdd/ai-coding-integration/test_controlled_commit.py`

**Interfaces:**
- Produces: `CodingIntegrationService(workspace_service)`
- Produces: `create_commit(workspace, repository, verification_evidence) -> CodingCommitResult`

- [ ] **Step 1: 写 commit RED 测试**

使用临时 Git repo/workspace，先通过真实 patch validator/apply 产生 diff。断言 commit 只有一个 parent 且为 `workspace.base_commit`、tree 包含最终批准文件、worktree commit 后 clean、author/committer 为配置身份、message 为服务端固定模板且不含用户 summary/source code。

- [ ] **Step 2: 写安全负面 RED 测试**

覆盖：integration disabled、verification evidence 为空或存在非 passed、workspace HEAD 漂移、workspace clean 无 diff、额外未批准变更、Git hook 不执行。commit 必须绑定最终 approved changed paths 集合，不能 `git add -A` 接纳未知文件。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_controlled_commit.py
```

- [ ] **Step 4: 实现 controlled commit**

Graph 向 service 传入最终 `changed_paths` 与全部 passed evidence。service 重新校验 HEAD、status 和 candidate
path 集合，拒绝任何额外 tracked/untracked dirty entry。它使用受管临时 index 从 `base_commit` 读取 tree，
只 `git add -- <approved paths>` 到该临时 index，再用 `git write-tree` 和 `git commit-tree` 创建单亲 commit，
最后以 compare-and-swap 更新 detached `HEAD`。整个过程不调用 porcelain commit，因此不会运行 hooks、
editor 或 signing；环境仍只设置受信 author/committer并禁用 prompt/system config。成功返回 source commit、
parent、tree、changed paths 和 verification evidence digest。若 crash 后重放，只有 clean HEAD 的
parent/message/author/tree 与 expected facts 全部匹配时才接受既有 controlled commit。

- [ ] **Step 5: 运行 GREEN 并提交 service 第一部分**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_controlled_commit.py
git add src/assistant_agent/coding/integration.py
git commit -m "feat: create verified coding commits"
```

---

### Task 3: 实现 Target Preflight、Merge Preview 与确定性 Apply

**Files:**
- Modify: `src/assistant_agent/coding/integration.py`
- Create: `tests/tdd/ai-coding-integration/test_merge_integration.py`

**Interfaces:**
- Produces: `prepare_merge(workspace, repository, commit) -> CodingMergePreview`
- Produces: `apply_merge(workspace, repository, preview) -> CodingMergeResult`

- [ ] **Step 1: 写 fast-forward RED 测试**

目标分支仍在 base 时，preview strategy 为 `fast_forward`、result commit 等于 source commit。apply 前目标不变；apply 后目标 HEAD/result tree 正确且 clean；相同 preview 重放返回同一结果。

- [ ] **Step 2: 写 diverged conflict-free RED 测试**

目标分支先提交不相交变更。preview 使用 merge-tree 生成 result tree 和双亲 result commit，审批前目标 HEAD/worktree 不变；apply 后目标只 fast-forward 到预生成 result commit，parents 精确为 expected target/source。

- [ ] **Step 3: 写 fail-closed RED 测试**

覆盖 target branch checkout 不匹配、target dirty、source commit 不匹配、merge conflict、preview 后 target HEAD 漂移、preview digest/commit/tree 篡改。每项断言目标 HEAD、status 和文件内容保持调用前事实；不产生 merge/rebase/cherry-pick 状态。

- [ ] **Step 4: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_merge_integration.py
```

- [ ] **Step 5: 实现 preflight 与 canonical digest**

在 repo-scoped process lock 内校验 resolved target path、symbolic branch、porcelain status、target ref 与 commit ancestry。FF 直接冻结 source tree/commit；非 FF 调用固定 `git merge-tree --write-tree expected source`，冲突时返回 `merge_conflict`，成功后用固定 author/message 的 `git commit-tree <tree> -p <target> -p <source>` 生成 result commit。preview digest 由排序 JSON 计算，不含路径、stderr 或时间。

- [ ] **Step 6: 实现 apply**

重新解析并校验 preview digest、source commit/tree、target branch/status/head。若 target 已等于 result commit且 clean，返回幂等成功；否则只执行禁用 hooks/config/signing 的 `git merge --ff-only <result_commit>`。命令失败后验证目标事实未变；任何不一致返回稳定错误并停止，不重算 preview。

- [ ] **Step 7: 运行 GREEN 并提交 service 第二部分**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_merge_integration.py
git add src/assistant_agent/coding/integration.py
git commit -m "feat: preview and apply controlled coding merges"
```

---

### Task 4: 接入 CodingGraph 独立 Merge HITL

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/coding_graph.py`
- Create: `tests/tdd/ai-coding-integration/test_integration_graph.py`

**Interfaces:**
- Changes: `build_coding_graph(..., integration_service)`
- Changes: `CodingState.commit_result`、`merge_preview`、`merge_result`
- Adds nodes: `create_commit -> prepare_merge -> merge_approval -> apply_merge`

- [ ] **Step 1: 写 Graph RED 测试**

使用真实 workspace/integration service、fake passing validation 与 `InMemorySaver`。先完成 patch approval，断言 validation 后创建 source commit与 preview，然后产生第二个 action=`coding_merge_apply` interrupt；payload 只含 bounded branch/strategy/source/target/result/preview digest。

- [ ] **Step 2: 写 merge approval RED 测试**

错误 source commit、expected target head 或 preview digest 分别返回 `merge_approval_mismatch` 且目标不变；reject 返回 terminal rejected 且目标不变；正确三元组批准后目标合并一次并返回 `status=merged`。preview 后外部推进 target 时 resume 返回 `target_head_changed`，不自动生成新 interrupt。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_integration_graph.py
```

- [ ] **Step 4: 扩展 Graph**

`run_validation` 成功且 repository `integration_enabled=false` 时保持阶段 2 applied terminal；启用时转 `create_commit`。commit/preview 都是确定性节点。`merge_approval` 只接受 `CodingMergeApprovalDecision`，拒绝 respond/edit；resume facts 必须与 checkpoint preview 全等。`apply_merge` 不调用模型，并把结构化 merge result 投影到 terminal。

- [ ] **Step 5: 运行 GREEN 并提交 Graph**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_integration_graph.py
git add src/assistant_agent/native_agent/state.py src/assistant_agent/native_agent/coding_graph.py
git commit -m "feat: add independent coding merge approval"
```

---

### Task 5: Production Composition 与核心不变量

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Create: `tests/tdd/ai-coding-integration/test_integration_composition.py`

**Interfaces:**
- Changes: process owner owns one `CodingIntegrationService`
- Changes: `LOOP-001` includes deterministic commit/preview/merge lane
- Changes: `CTX-001` explicitly includes independent merge HITL bound to frozen preview

- [ ] **Step 1: 写 composition RED 测试**

断言 owner 显式持有 integration service并注入唯一 coding graph；子图包含四个 integration nodes；fast/planning/coding Tool inventory 均无 commit/merge/push Tool；disabled/default repository 不创建 commit或 merge side effect。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_integration_composition.py
```

- [ ] **Step 3: 装配 service 与更新 core**

composition 用同一 `CodingWorkspaceService` 构造 integration service并纳入 `aclose()`。`LOOP-001` 的 coding 节点集合加入四节点；`CTX-001` 文本登记 merge preview 独立 HITL。只扩展现有 core 结构断言，不新增 core 测试文件或自然语言断言。

- [ ] **Step 4: 运行 GREEN 与受影响 core并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration/test_integration_composition.py tests/core/integration/test_runtime_lifecycle.py
git add src/assistant_agent/agent_server/services.py tests/core/INVARIANTS.md tests/core/integration/test_runtime_lifecycle.py
git commit -m "feat: compose controlled coding integration"
```

---

### Task 6: 同步 Authority 与完成验收

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/authority.toml`

- [ ] **Step 1: 更新 owner authority**

Agent Server 记录 integration service、repo lock、目标 preflight与 Git object生命周期；runtime 记录 validation后 commit/preview/merge HITL拓扑；tool authority 明确 commit/merge不是模型 Tool且 push/PR仍不存在。manifest 将 `src/assistant_agent/coding/integration.py` 路由到 agent-server，并登记阶段3定向 TDD。

- [ ] **Step 2: 运行阶段 3 定向验收**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-integration tests/core/integration/test_runtime_lifecycle.py
```

- [ ] **Step 3: 运行阶段 1/2 最小回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-patch tests/tdd/ai-coding-workspace
```

阶段 2 临时 TDD 位于主工作区 ignored 目录，不存在于本 worktree；合并后再运行。

- [ ] **Step 4: 运行 authority validator 与完整 core**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

- [ ] **Step 5: 合并后验证现有 8089 hot reload**

只作为客户端请求用户管理的 8089；不启动第二套 Server。默认 integration disabled 做兼容 smoke，完整 merge副作用只在临时本地 repo TDD中执行，不对本项目主分支做自合并测试。

- [ ] **Step 6: 提交 authority**

```bash
git add docs/agent-server-architecture.md docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md docs/authority.toml
git commit -m "docs: document controlled coding integration"
```

## 完成汇报格式

```text
完成：AI Coding 阶段 3 受控 commit 与合并。
Core invariant: LOOP-001 extended with deterministic commit/preview/apply; CTX-001 extended with frozen-preview merge HITL.
Tests: added tests/tdd/ai-coding-integration for temporary RED/GREEN; user may delete this directory manually.
Validation: <实际命令与结果>。
Provider: 未调用真实 Provider；全部验证使用 mock/offline。
Limitations: 不 push、不创建 PR、不 fetch/pull、不使用远程凭据、不自动修复冲突；强 sandbox仍属于阶段4。
```
