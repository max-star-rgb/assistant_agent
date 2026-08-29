# Native Worktree Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 Deep Agents 原生 filesystem/execute/HITL 保留 thread worktree，并把每个 worktree 的未提交增量安全回灌为本地主工作区的未提交改动，同时删除退出生产链的旧 coding framework。

**Architecture:** 使用窄 `assistant_agent.worktree` 包管理 thread-scoped Git worktree，并由 Deep Agents 原生 backend 提供 filesystem/execute。`apply_worktree_changes` 用临时 index 快照双方未提交状态，以无引用 `commit-tree` 对象调用 `merge-tree`；clean merge 直接回灌，文本冲突在 thread worktree 中形成解决会话，最终仍以未暂存 patch 写入主工作区。

**Tech Stack:** Python 3.12、Git CLI、Pydantic、Deep Agents 0.7.8、LangChain `BaseTool`/`ToolRuntime`、LangGraph HITL、pytest。

**Spec:** `docs/superpowers/specs/2026-08-28-native-worktree-delivery-design.md`

## Global Constraints

- 主 Agent 使用可写 thread worktree；同步和异步 worker 保持只读。
- `apply_worktree_changes` 的 source、target、identity、thread 与 repository 全部由服务端决定，模型不传宿主路径。
- 回灌前目标 HEAD 必须等于 source `base_commit`；目标可以含此前成功回灌的未提交改动。
- snapshot/merge 必须覆盖新增、修改、删除、rename、mode 与二进制文件；使用 Git `merge-tree`，不实现自研合并算法，不使用 `--reject`。
- `commit-tree` 只能创建没有 ref 的临时 object；不得创建可达用户 commit、移动 HEAD/branch 或改变正常 `git log`。
- 文本冲突在 thread worktree 中解决；二进制、submodule 和不可编辑冲突只结构化报告并保留人工处理。
- 冲突解决回灌前必须复核目标 affected paths 的 mode/blob fingerprint，不能覆盖并发的人类修改。
- 回灌后目标 HEAD 不变，改动未暂存、未 commit；不自动 stash、reset、commit、push、创建分支或 PR。
- 当前 `LocalShellBackend` 不是安全 sandbox；本轮不新增 container/VM/remote sandbox。
- 默认 mock/offline，不调用真实 Provider。
- Git 最低版本为 2.43，必须支持 `merge-tree --write-tree --messages -z --merge-base=<commit>`；不满足时明确失败。
- 保留用户当前工作区中的无关修改；所有删除和提交只包含本任务文件。

---

### Task 1: 建立最小 thread worktree manager 与原生 backend

**Files:**
- Create: `src/assistant_agent/worktree/__init__.py`
- Create: `src/assistant_agent/worktree/manager.py`
- Create: `src/assistant_agent/worktree/backend.py`
- Create: `tests/tdd/native-worktree-delivery/test_manager_backend.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/agent_server/async_delegation.py`
- Modify: `tests/tdd/unified-assistant-agent/test_async_snapshot.py`
- Modify: `tests/tdd/unified-assistant-agent/test_read_only_worker.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`

**Interfaces:**
- Produces: `WorktreeConfig`, `WorktreeRepository`, `ThreadWorktree`, `ThreadWorktreeError`, `ThreadWorktreeManager.resolve()`, `ThreadWorktreeManager.repository_head()`。
- Produces: `ThreadWorktreeBackend` implementing `SandboxBackendProtocol` and `ReadOnlyThreadWorktreeBackend` implementing `BackendProtocol`。
- Consumes: `AssistantRuntimeFacts.repository_snapshot_sha` and Agent Server authenticated identity/thread metadata。

- [ ] **Step 1: 编写 manager/backend RED 测试**

```python
def test_threads_resolve_distinct_worktrees_from_the_same_head(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    manager = ThreadWorktreeManager(config(tmp_path, repo))

    first = manager.resolve("user", "thread-1", "repo")
    second = manager.resolve("user", "thread-2", "repo")

    assert first.root != second.root
    assert first.base_commit == second.base_commit == git(repo, "rev-parse", "HEAD")


def test_explicit_snapshot_stays_on_original_commit(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    original = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "later.txt", b"later")

    workspace = ThreadWorktreeManager(config(tmp_path, repo)).resolve(
        "user", "worker-thread", "repo", base_commit=original
    )

    assert git(workspace.root, "rev-parse", "HEAD") == original
```

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery/test_manager_backend.py
```

Expected: FAIL because `assistant_agent.worktree` does not exist.

- [ ] **Step 3: 实现最小 manager schema 与生命周期**

```python
class WorktreeRepository(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repo_id: str
    path: Path


class WorktreeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_root: Path
    repositories: dict[str, WorktreeRepository]
    ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)


class ThreadWorktree(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_ref: str
    root: Path
    repo_id: str
    base_commit: str
    expires_at: datetime


class ThreadWorktreeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
```

`ThreadWorktreeManager.resolve(identity, thread_id, repo_id, *, base_commit=None)` 使用确定性摘要目录、server-owned metadata、`git worktree add --detach` 和文件锁。已有 workspace 必须复核 identity/thread/repo/base；`repository_head(repo_id)` 只返回受信 repository 当前 commit。TTL 清理通过 `git worktree remove --force` 处理过期目录，不复制旧 analysis snapshot/reaper/policy 代码。

- [ ] **Step 4: 实现 Deep Agents backend 并迁移装配引用**

```python
class ThreadWorktreeBackend(SandboxBackendProtocol):
    def _backend(self) -> LocalShellBackend:
        workspace = _runtime_worktree(self._manager, self._repo_id)
        return LocalShellBackend(
            root_dir=workspace.root,
            virtual_mode=True,
            timeout=120,
            max_output_bytes=100_000,
            env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            inherit_env=False,
        )


class ReadOnlyThreadWorktreeBackend(BackendProtocol):
    def _backend(self) -> FilesystemBackend:
        return FilesystemBackend(
            root_dir=_runtime_worktree(self._manager, self._repo_id).root,
            virtual_mode=True,
        )
```

保留现有 async worker 规则：只有真实 graph ID `assistant-worker-v2` 且 `entry_profile=async_worker` 时接受冻结 snapshot；main/sync worker 拒绝 snapshot 注入。

- [ ] **Step 5: 运行 manager、async snapshot 与 worker 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery/test_manager_backend.py \
  tests/tdd/unified-assistant-agent/test_async_snapshot.py \
  tests/tdd/unified-assistant-agent/test_read_only_worker.py
```

Expected: PASS.

### Task 2: 实现 patch 回灌与原生 HITL Tool

**Files:**
- Create: `src/assistant_agent/worktree/tools.py`
- Create: `tests/tdd/native-worktree-delivery/test_apply_changes.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/native_agent/tool_profiles.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: `ThreadWorktreeManager.resolve()` and `WorktreeRepository.path` from Task 1。
- Produces: `apply_changes(manager, identity, thread_id, repo_id) -> WorktreeApplyResult` and `create_apply_worktree_changes_tool(manager, repo_id) -> BaseTool`；Tool name 为 `apply_worktree_changes`，且 `metadata.effect == "dangerous"`。
- Produces artifact fields: `status`, `workspace_ref`, `base_commit`, `target_head`, `patch_digest`, `changed_paths`。

- [ ] **Step 1: 编写 patch 回灌 RED 测试**

```python
def test_apply_keeps_target_head_and_leaves_changes_uncommitted(tmp_path: Path) -> None:
    repo, manager, workspace = prepared_workspace(tmp_path)
    (workspace.root / "tracked.txt").write_text("changed\n")
    (workspace.root / "new.bin").write_bytes(b"\x00\x01")

    result = apply_worktree_changes(manager, "user", "thread", "repo")

    assert git(repo, "rev-parse", "HEAD") == workspace.base_commit
    assert (repo / "tracked.txt").read_text() == "changed\n"
    assert (repo / "new.bin").read_bytes() == b"\x00\x01"
    assert git(repo, "status", "--porcelain")
```

同时添加：删除文件、mode 变化、两个非冲突 thread 累积、重叠 patch 失败且不部分写入、目标 HEAD 漂移失败、空 patch 返回 `no_changes`。

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery/test_apply_changes.py
```

Expected: FAIL because apply helper and Tool do not exist.

- [ ] **Step 3: 用临时 index 实现 patch 提取与应用**

```python
with TemporaryDirectory(dir=manager.config.workspace_root) as temporary:
    index = Path(temporary) / "index"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    run_git(source.root, "read-tree", source.base_commit, env=env)
    run_git(source.root, "add", "-A", "--", env=env)
    changed_paths = nul_paths(run_git_bytes(source.root, "diff", "--cached", "--name-only", "-z", source.base_commit, env=env))
    patch = run_git_bytes(source.root, "diff", "--cached", "--binary", "--full-index", source.base_commit, env=env)

run_git_bytes(target, "apply", "--check", "-", input_bytes=patch)
run_git_bytes(target, "apply", "-", input_bytes=patch)
```

对每个 target repository 使用进程内窄锁包住 HEAD/status 检查、`apply --check` 和 `apply`。错误映射为稳定 code：`worktree_base_changed`、`worktree_no_changes`、`worktree_apply_conflict`、`worktree_apply_failed`。

- [ ] **Step 4: 创建标准 BaseTool 并接入 composition**

```python
@tool("apply_worktree_changes", response_format="content_and_artifact")
def apply_worktree_changes(runtime: ToolRuntime[AssistantRunContext]):
    identity = authenticated_user_identity(runtime)
    thread_id = str(runtime.config.get("configurable", {}).get("thread_id", "")).strip()
    result = apply_changes(manager, identity, thread_id, repo_id)
    artifact = result.model_dump(mode="json")
    return json.dumps(artifact, ensure_ascii=False), artifact

configured = configure_builtin_tool(apply_worktree_changes, "dangerous")
```

在创建 `ThreadWorktreeManager` 后把 Tool 追加到 main inventory；worker 仍只筛选 `effect=read`。把 Tool 加入 `filesystem` profile，使模型先激活该 profile；统一 `_interrupt_on()` 自动把它纳入 HITL。

- [ ] **Step 5: 运行 patch 与装配测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS，且 main Tool inventory 含 `apply_worktree_changes`、worker inventory 不含它、`interrupt_on` 含它。

### Task 3: 删除旧 coding framework 与迁移当前文档

**Files:**
- Delete: `src/assistant_agent/coding/`
- Delete: `src/assistant_agent/native_agent/coding_phase.py`
- Delete: `tests/tdd/deepagents-coding-agent/`
- Modify: `.env.example`
- Modify: `docs/authority.toml`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: all replacement imports and types delivered by Tasks 1-2。
- Produces: no runtime import of `assistant_agent.coding` or `native_agent.coding_phase`。

- [ ] **Step 1: 删除明确退出生产链的文件和旧临时测试**

使用 `apply_patch` 删除设计规格中列出的 coding modules、`coding_phase.py` 与用户已明确授权删除的 `tests/tdd/deepagents-coding-agent/`。删除 `.env.example` 中 `ASSISTANT_AGENT_CODING_SANDBOX_IMAGE` 及旧 sandbox protocol 注释。

- [ ] **Step 2: 更新 authority 与当前架构名称**

把 source globs 和 authority contract 中的：

```text
src/assistant_agent/coding/backend.py   -> src/assistant_agent/worktree/**
CodingWorkspaceService                 -> ThreadWorktreeManager
CodingWorkspaceBackend                 -> ThreadWorktreeBackend
ReadOnlyCodingWorkspaceBackend         -> ReadOnlyThreadWorktreeBackend
```

同步记录 `apply_worktree_changes` 的 patch 回灌语义以及“worktree 不是 sandbox”。不得把设计/计划文档提升为当前 authority。

- [ ] **Step 3: 扫描残留引用**

Run:

```bash
rg -n 'assistant_agent\.coding|CodingWorkspace|coding sandbox|coding_phase' \
  src tests/core tests/tdd docs/*.md .env.example
```

Expected: no obsolete production or authority references；仅允许描述“没有 coding mode”的普通文字。

- [ ] **Step 4: 运行 import/compile 与文档校验**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/worktree src/assistant_agent/agent_server
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: both commands exit 0.

### Task 4: 完整验证、hot reload 与任务交付

**Files:**
- Modify if required by failures: only files already owned by Tasks 1-3
- Keep uncommitted by project rule: `docs/superpowers/specs/2026-08-28-native-worktree-delivery-design.md`
- Keep uncommitted by project rule: `docs/superpowers/plans/2026-08-28-native-worktree-delivery.md`

**Interfaces:**
- Consumes: completed implementation from Tasks 1-3。
- Produces: verified native worktree delivery implementation and an exact task-scoped diff inventory。

- [ ] **Step 1: 运行 feature 与受影响 core tests**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery \
  tests/tdd/unified-assistant-agent \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py
```

Expected: PASS in mock/offline mode.

- [ ] **Step 2: 复跑文档 authority validator**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: PASS.

- [ ] **Step 3: 验证现有 8089 dev server hot reload**

Run:

```bash
curl --fail --silent --show-error http://127.0.0.1:8089/ok
```

Expected: server returns a successful health response；不得启动第二个 dev server。若 8089 原本未运行，报告该限制而不自行启动并行实例。

- [ ] **Step 4: 检查 diff 边界并决定提交**

Run:

```bash
git status --short
git diff --check
git diff --stat
```

保持本计划源码、测试、authority、spec 与 plan 全部未提交；不暂存用户已有无关修改。用户已经明确要求由其手动
commit/push，执行 Agent 不得创建 commit。

- [ ] **Step 5: 最终汇报**

报告删除/重命名、新 Tool 行为、实际验证命令、8089 reload 状态、未提交 spec/plan，以及：

```text
Core invariant: LOOP-001 and CTX-001 updated for native thread worktree delivery.
Tests: added tests/tdd/native-worktree-delivery for temporary RED/GREEN; user may delete the directory manually.
```

---

以下为 2026-08-28 已确认的冲突主动修复增量；Tasks 1-4 是已经完成的基础实现，后续从 Task 5 开始。

### Task 5: 用 Git snapshot 与 merge-tree 替换直接 patch 冲突判定

**Files:**
- Modify: `src/assistant_agent/worktree/manager.py`
- Modify: `tests/tdd/native-worktree-delivery/test_apply_changes.py`

**Interfaces:**
- Consumes: `ThreadWorktreeManager.resolve(identity, thread_id, repo_id)`、`ThreadWorktree.base_commit`、server-owned `WorktreeRepository.path`。
- Produces: `WorktreeApplyResult.status: Literal["applied", "no_changes", "conflict"]`、`conflicting_paths: tuple[str, ...]`、`resolution_id: str | None`。
- Private snapshot contract: `_WorkingTreeSnapshot(tree_oid, commit_oid, changed_paths, path_fingerprints)`；临时 commit 的 parent 固定为 `base_commit`，不创建 ref。

- [ ] **Step 1: 添加 clean three-way merge RED 测试**

```python
def test_non_overlapping_changes_in_the_same_file_are_merged(tmp_path: Path) -> None:
    manager, target, base = prepared_manager(tmp_path, "first\nsecond\n")
    source = manager.resolve("user", "thread", "repo")
    (target / "tracked.txt").write_text("MAIN\nsecond\n", encoding="utf-8")
    (source.root / "tracked.txt").write_text("first\nAGENT\n", encoding="utf-8")
    refs_before = git(target, "for-each-ref", "--format=%(refname):%(objectname)")

    result = manager.apply_changes("user", "thread", "repo")

    assert result.status == "applied"
    assert (target / "tracked.txt").read_text(encoding="utf-8") == "MAIN\nAGENT\n"
    assert git(target, "rev-parse", "HEAD") == base
    assert git(target, "diff", "--cached", "--name-only") == ""
    assert git(target, "for-each-ref", "--format=%(refname):%(objectname)") == refs_before
```

同时断言新增/删除、rename、mode 和 binary clean merge 仍保留；目标真实 index 在调用前后字节语义不变。

- [ ] **Step 2: 运行 clean merge RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery/test_apply_changes.py \
  -k 'same_file or rename or mode or binary'
```

Expected: 当前直接 `git apply --check` 对同文件非重叠主工作区修改失败，至少 `same_file` 用例 FAIL。

- [ ] **Step 3: 实现双方 working tree snapshot**

在 `manager.py` 内用同一 Git repository object database 和两个独立临时 index：

```python
@dataclass(frozen=True)
class _WorkingTreeSnapshot:
    tree_oid: str
    commit_oid: str
    changed_paths: tuple[str, ...]
    path_fingerprints: dict[str, str]


def _snapshot(self, root: Path, base_commit: str, index: Path) -> _WorkingTreeSnapshot:
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    self._git_bytes(root, "read-tree", base_commit, env=env)
    self._git_bytes(root, "add", "-A", env=env)
    tree = self._git(root, "write-tree", env=env)
    commit = self._commit_tree(root, tree, base_commit)
    changed = self._changed_paths(root, base_commit, tree)
    return _WorkingTreeSnapshot(tree, commit, changed, self._fingerprints(root, tree, changed))
```

扩展内部 Git runner 支持 `env` 与 `input_bytes`，不输出 raw stderr。`_commit_tree()` 显式设置 server-owned
author/committer name、email 和固定格式 message，执行 `git commit-tree <tree> -p <base> -m ...`；不得调用
`update-ref`、`checkout`、`commit` 或修改真实 index。

- [ ] **Step 4: 实现 merge-tree clean 路径**

```python
completed = self._run_git(
    target,
    "merge-tree",
    "--write-tree",
    "--name-only",
    "--messages",
    "-z",
    f"--merge-base={base_commit}",
    target_snapshot.commit_oid,
    source_snapshot.commit_oid,
)
```

按本机 Git 2.43 contract 处理：exit `0` 为 clean、`1` 为 conflict、其他为执行错误；stdout 首个 NUL 字段是 result
tree。clean 时生成 `git diff --binary --full-index <target_tree> <result_tree>`，锁内重新 snapshot 目标 affected
paths 并比对 fingerprint，再 `git apply --check`、`git apply`。patch 为空返回 `status="no_changes"`。

- [ ] **Step 5: 运行 clean merge GREEN 与原回灌回归**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery/test_apply_changes.py
```

Expected: PASS；HEAD/ref/index 不变，非重叠修改合并为 `A+B1+B2`。

### Task 6: 建立结构化冲突预览与 Agent 修复回灌

**Files:**
- Modify: `src/assistant_agent/worktree/manager.py`
- Modify: `src/assistant_agent/worktree/tools.py`
- Modify: `tests/tdd/native-worktree-delivery/test_apply_changes.py`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Consumes: Task 5 `_WorkingTreeSnapshot` 与 `merge-tree --name-only --messages -z` 输出。
- Produces: repo 外 `<workspace>/resolution.json`，字段固定为 `resolution_id/base_commit/target_tree/merge_tree/source_changed_paths/conflicting_paths/target_path_fingerprints/preview_path_fingerprints`。
- Produces: 同一个零模型参数 `apply_worktree_changes` Tool；首次冲突返回结构化 `status="conflict"`，Agent 编辑预览后再次调用完成回灌。

- [ ] **Step 1: 添加 conflict preview RED 测试**

```python
def test_text_conflict_is_materialized_only_in_thread_worktree(tmp_path: Path) -> None:
    manager, target, _ = prepared_manager(tmp_path, "base\n")
    source = manager.resolve("user", "thread", "repo")
    (target / "tracked.txt").write_text("main\n", encoding="utf-8")
    (source.root / "tracked.txt").write_text("agent\n", encoding="utf-8")

    result = manager.apply_changes("user", "thread", "repo")

    assert result.status == "conflict"
    assert result.conflicting_paths == ("tracked.txt",)
    assert result.resolution_id
    assert (target / "tracked.txt").read_text(encoding="utf-8") == "main\n"
    preview = (source.root / "tracked.txt").read_text(encoding="utf-8")
    assert "<<<<<<<" in preview and "=======" in preview and ">>>>>>>" in preview
```

另加测试：Tool 冲突返回不是 `ToolException`；binary conflict 返回 `status="conflict"` 但不物化文本预览；manifest
位于 `source.root.parent` 而非 Git repo 内；模型可见 Tool schema 仍为空。

- [ ] **Step 2: 运行 conflict preview RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery/test_apply_changes.py -k conflict
```

Expected: 当前实现抛出 `worktree_apply_conflict`，测试 FAIL。

- [ ] **Step 3: 解析 merge-tree 冲突并物化文本预览**

使用 `--name-only --messages -z`：首字段为 merge result tree；conflicted-file section 提供精确路径；空 NUL 分隔后
的 message records 提供稳定 conflict type 与相关路径。必须以 exit status 判断是否 conflict，不能以路径列表是否为空
判断。对可编辑文本冲突，生成 `<source_snapshot.tree> → <merge_result.tree>` 的 affected-path binary patch，并只应用
到 thread worktree；主工作区不执行任何 apply。binary/submodule/directory-file 等不可编辑类型只返回路径与摘要。

将 `_ResolutionManifest` 以 Pydantic frozen model 原子写入 `source.root.parent / "resolution.json"`。`resolution_id`
使用 manifest canonical JSON 的 SHA-256；所有返回值只含 repo-relative paths 和 object digest，不含绝对路径。

- [ ] **Step 4: 添加 resolution retry RED 测试**

```python
def test_agent_resolution_applies_only_after_target_fingerprint_check(tmp_path: Path) -> None:
    manager, target, base = conflicting_manager(tmp_path)
    source = manager.resolve("user", "thread", "repo")
    conflict = manager.apply_changes("user", "thread", "repo")
    (source.root / "tracked.txt").write_text("main + agent\n", encoding="utf-8")

    applied = manager.apply_changes("user", "thread", "repo")

    assert applied.status == "applied"
    assert (target / "tracked.txt").read_text(encoding="utf-8") == "main + agent\n"
    assert git(target, "rev-parse", "HEAD") == base
    assert git(target, "diff", "--cached", "--name-only") == ""
    assert not (source.root.parent / "resolution.json").exists()
```

另加：预览未修改返回 `worktree_resolution_incomplete`；冲突后人类再次修改 affected target path 返回
`worktree_target_snapshot_changed` 且目标保持原样；修改无关 target path 不阻止解决回灌；过期 worktree 清理同时删除
manifest。

- [ ] **Step 5: 实现 resolution retry**

检测有效 manifest 后，不重新执行初始 merge。先验证 identity/thread/repo/base 与 resolution ID；要求每个可编辑冲突
路径当前 source fingerprint 不等于 preview fingerprint。以 `merge_tree` 初始化临时 index，再通过
`git add -A --pathspec-from-file=- --pathspec-file-nul` 从 source working tree 覆盖原始 changed paths，`write-tree`
得到 resolved result tree。生成 `<target_tree> → <resolved_tree>` binary/full-index patch。

持有 target repo 锁时验证 HEAD 仍为 base，并仅对 manifest affected paths 重算 mode/blob fingerprint；不相关路径变化
允许继续。验证和 `git apply --check` 全部成功后才执行一次 `git apply`，随后删除 manifest。任何失败不得修改目标，
不得使用 conflict marker 字符串扫描代替 snapshot 校验。

- [ ] **Step 6: 更新 Tool 投影与 authority**

`WorktreeApplyResult.model_dump(mode="json")` 直接返回 `applied/no_changes/conflict`。Tool 仅把 Git/IO/身份错误映射为
`ToolException`；冲突是 Agent 可处理的正常结构化结果。更新三份当前 authority，记录临时 commit object 不创建 ref、
冲突预览只写 thread worktree、最终写主工作区仍需第二次 dangerous Tool HITL。

- [ ] **Step 7: 运行增量与完整验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-worktree-delivery

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/unified-assistant-agent

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/worktree tests/tdd/native-worktree-delivery
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8089/ok
git diff --check
```

Expected: 三组 pytest、ruff、authority validator、diff check 均通过，8089 返回 `{"ok":true}`；保持主分支 HEAD 与
真实 index 不变，所有本任务改动未提交，未调用真实 Provider。
