# 统一 Assistant Agent 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 fast、planning、coding 三条生产路线替换为一个能直接回答、规划、委派、读写仓库和执行命令，并在全部副作用前请求审批的统一 Assistant Agent。

**Architecture:** 父图只保留 Memory recall、单个 `AssistantAgent` 子图和 delayed extraction；主循环由 Deep Agents `create_deep_agent` 编译。同步 `general-purpose` 与 `assistant-worker-v2` 复用一个窄 `create_agent` 只读 worker，通过 Tool surface、backend capability 和 state 双向 allowlist 实现隔离；主 Agent 独占 thread worktree 写入和宿主机 `execute`。

**Tech Stack:** Python 3.12、LangChain 1.3.15、LangGraph 1.2.x、Deep Agents 0.7.8、Pydantic 2、LangGraph Agent Server、pytest。

**Spec:** `docs/superpowers/specs/2026-08-27-unified-assistant-agent-design.md`

## Global Constraints

- 默认 Python 固定为 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- 所有开发测试固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，本计划不得调用真实 Provider。
- 主 Agent 必须由一次 `create_deep_agent` 构造；不得新增自研 Agent loop、scheduler、Tool executor 或 checkpoint adapter。
- 简单请求允许直接回答；`write_todos` 和 `task` 由模型按需调用，不得由入口关键词路由。
- 模型文件 Tool 只有 thread-scoped Git worktree 一套视图；不得使用 `CompositeBackend` 或回退到项目主目录。
- `write_file`、`edit_file`、`delete`、`execute`、`effect=write|dangerous|generate`、异步任务写操作和非只读 MCP 必须在 handler 前进入原生 HITL。
- `LocalShellBackend` 只用于受信本地单用户环境；HITL 是治理边界而非进程隔离，批准 `execute` 等价于授权 Agent Server OS identity 执行完整命令。
- 同步与异步 worker 只读；主 Agent 是唯一写入者。Worker 输入、输出 state 都使用正向 allowlist。
- async task 创建时固定 `repository_snapshot_sha`；worker 缺少或不匹配 SHA 时不得回退当前 HEAD。
- Graph ID 固定升级为 `assistant-native-v4`、`assistant-worker-v2`；`assistant-memory-v1` 不变，不保留旧 alias。
- 所有受控客户端直接删除 `assistantMode`、`execution_mode` 与 fast/planning 命令，不实现接收后忽略的兼容层。
- 临时 RED/GREEN 测试只写入 `tests/tdd/unified-assistant-agent/`；永久测试只修改既有 `LOOP-001`、`CTX-001`、`MEMORY-001`、`GATE-001`、`IDENT-001` 负责文件。
- 不重建绑定旧 CodingGraph patch/review/merge state 的 `ai_coding_behavior` runner；删除该失效 runner，未来行为基线另立 spec。
- 本计划与设计 spec 保持未跟踪，不加入实施提交；每次提交只包含对应任务的源码、测试或当前 authority。

## 文件结构

### 新建

- `src/assistant_agent/native_agent/assistant_agent.py`：统一主 Agent、只读 worker、同步 task state 投影和共用 middleware 装配。
- `src/assistant_agent/coding/backend.py`：thread worktree 的可写 Shell backend 与无写能力只读 backend。
- `tests/tdd/unified-assistant-agent/test_async_snapshot.py`：创建时 SHA 固定与 metadata 传播。
- `tests/tdd/unified-assistant-agent/test_read_only_worker.py`：Worker Tool surface、backend capability、state 双向隔离。
- `tests/tdd/unified-assistant-agent/test_unified_graph.py`：统一 main Agent、HITL 和父图拓扑。
- `tests/tdd/unified-assistant-agent/test_client_contract.py`：mode 字段与旧 graph identity 删除。

### 修改

- `src/assistant_agent/coding/workspace.py`：允许按显式 commit 创建 detached worktree。
- `src/assistant_agent/native_agent/context.py`：公开 context 只保留 `enable_memory`；私有 runtime facts 增加 snapshot SHA。
- `src/assistant_agent/native_agent/state.py`：统一主 state、只读 worker state 和 async task reducer。
- `src/assistant_agent/native_agent/root_graph.py`：删除 router，改成唯一 `assistant_agent` 节点。
- `src/assistant_agent/native_agent/providers.py`：删除 mock 的 planning 强制 task 行为，增加只读 worker model view 命名。
- `src/assistant_agent/coding/review.py`：接回仅由 coding review schema 使用的 attestation reducer，避免反向依赖已删除的 graph state。
- `src/assistant_agent/native_agent/tool_profiles.py`：`filesystem` Profile 纳入 `execute`。
- `src/assistant_agent/skills/native.py`：Skills discovery 使用固定项目 `FilesystemBackend`；自定义 FS middleware 只供 worker allowlist。
- `src/assistant_agent/agent_server/async_delegation.py`：捕获、持久化和复用 `repository_snapshot_sha`，为异步 Tool 标记 effect。
- `src/assistant_agent/agent_server/services.py`：只构造 unified main、read-only worker、Memory graph。
- `src/assistant_agent/agent_server/config.py`、`src/assistant_agent/agent_server/auth.py`、`src/assistant_agent/agent_server/graph.py`、`langgraph.json`：v4/v2 identity 与失效 eval surface 清理。
- `src/assistant_agent/agent_server/media_protocol.py`、`src/assistant_agent/agent_server/media_app.py`、`scripts/agent_cli.py`、`scripts/media_simulator.py`：删除客户端 mode。
- `src/assistant_agent/evaluation/native_graph_target.py`：统一 graph 调用签名。
- `evals/system/tools/native_tool.py`：孤立 ToolNode harness 改用标准 `AgentState` 和 metadata，不再引用 fast state。
- `tests/core/integration/test_runtime_lifecycle.py`、`tests/core/integration/test_context_lifecycle.py`、`tests/core/integration/test_memory_lifecycle.py`：更新 LOOP/CTX/MEMORY 覆盖。
- `tests/core/contract/test_tool_contract.py`、`tests/core/contract/test_observability_contract.py`、`tests/core/contract/test_gateway_contract.py`：更新 builder 与 v4/v2 契约。
- `tests/core/INVARIANTS.md`：重写 `LOOP-001`、`CTX-001`、`GATE-001`、`IDENT-001`。
- `README.md`、`docs/authority.toml`、`docs/runtime-event-stream-architecture.md`、`docs/tool-calling-architecture.md`、`docs/agent-server-architecture.md`、`docs/media-agent-service-websocket.md`、`docs/context_engineering_status.md`、`docs/visual-perception-architecture.md`、`evals/README.md`、`scripts/README.md`：同步当前 authority 和导航。

### 删除

- `src/assistant_agent/native_agent/fast_agent.py`
- `src/assistant_agent/native_agent/planning_agent.py`
- `src/assistant_agent/native_agent/coding_agent.py`
- `src/assistant_agent/native_agent/coding_graph.py`
- `src/assistant_agent/agent_server/attestation.py`
- `src/assistant_agent/evaluation/coding_agent_server.py`
- `src/assistant_agent/evaluation/coding_behavior.py`
- `evals/system/ai_coding_behavior/`
- `scripts/run_system_ai_coding_behavior_eval.py`

---

### Task 1: 固定 async worker 的 repository SHA

**Files:**
- Create: `tests/tdd/unified-assistant-agent/test_async_snapshot.py`
- Modify: `src/assistant_agent/coding/workspace.py`
- Modify: `src/assistant_agent/native_agent/context.py`
- Modify: `src/assistant_agent/agent_server/async_delegation.py`

**Interfaces:**
- Consumes: `CodingConfig.repositories[repo_id].path`、现有 `CodingWorkspaceService.git_head()`、Agent Server thread/run metadata。
- Produces: `CodingWorkspaceService.repository_head(repo_id: str) -> str`；`CodingWorkspaceService.resolve(identity: str, thread_id: str, repo_id: str, *, base_commit: str | None = None) -> CodingWorkspace`；`AssistantRuntimeFacts.repository_snapshot_sha: str | None`；`build_async_subagent_middleware(workspace_service, repo_id)`。

- [ ] **Step 1: 写出 worktree 固定 SHA 的失败测试**

```python
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    return repo


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _coding_config(tmp_path: Path, repo: Path) -> CodingConfig:
    return CodingConfig(
        enabled=True,
        workspace_root=tmp_path / "workspaces",
        repositories={
            "repo-sentinel": CodingRepositoryConfig(
                repo_id="repo-sentinel",
                path=repo,
                target_branch="main",
            )
        },
    )


def test_explicit_base_commit_survives_source_head_movement(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    first = _commit(repo, "first.txt", "first")
    service = CodingWorkspaceService(_coding_config(tmp_path, repo))
    second = _commit(repo, "second.txt", "second")

    workspace = service.resolve(
        "user-sentinel",
        "worker-thread-sentinel",
        "repo-sentinel",
        base_commit=first,
    )

    assert second != first
    assert service.git_head(workspace.root) == first
    assert workspace.base_commit == first


def test_existing_workspace_rejects_a_different_base_commit(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    first = _commit(repo, "first.txt", "first")
    service = CodingWorkspaceService(_coding_config(tmp_path, repo))
    service.resolve("user-sentinel", "thread-sentinel", "repo-sentinel",
                    base_commit=first)
    second = _commit(repo, "second.txt", "second")

    with pytest.raises(CodingWorkspaceError) as exc_info:
        service.resolve("user-sentinel", "thread-sentinel", "repo-sentinel",
                        base_commit=second)

    assert exc_info.value.code == "workspace_base_commit_mismatch"


def test_explicit_base_commit_must_exist_in_repository(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    _commit(repo, "first.txt", "first")
    service = CodingWorkspaceService(_coding_config(tmp_path, repo))

    with pytest.raises(CodingWorkspaceError) as exc_info:
        service.resolve("user-sentinel", "thread-sentinel", "repo-sentinel",
                        base_commit="f" * 40)

    assert exc_info.value.code == "workspace_base_commit_invalid"
```

- [ ] **Step 2: 运行测试确认当前实现读取了移动后的 HEAD**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_async_snapshot.py -k base_commit
```

Expected: FAIL；`resolve()` 不接受 `base_commit` 或 workspace HEAD 等于第二个 commit。

- [ ] **Step 3: 最小扩展 `CodingWorkspaceService`**

```diff
 def repository_head(self, repo_id: str) -> str:
    if not self.config.enabled or repo_id not in self.config.repositories:
        raise CodingWorkspaceError("workspace_not_allowed")
    return self.git_head(self.config.repositories[repo_id].path)

-def resolve(self, identity: str, thread_id: str, repo_id: str) -> CodingWorkspace:
+def resolve(self, identity: str, thread_id: str, repo_id: str, *,
+            base_commit: str | None = None) -> CodingWorkspace:
```

保留现有 `resolve()` 主体，只改四处：显式 SHA 校验、已有 metadata 的 SHA 一致性检查、创建 worktree 时使用解析后的 SHA、把该 SHA 写入 metadata。不要复制一套新的 resolve 流程。

在创建 worktree 前把 `base_commit or self.git_head(source_repo)` 解析为完整 SHA。显式值先以 `^[0-9a-f]{40,64}$` 校验，再执行：

```python
verified = self._run_git(
    source_repo,
    "rev-parse",
    "--verify",
    f"{base_commit}^{{commit}}",
    error_code="workspace_base_commit_invalid",
).strip()
if verified != base_commit:
    raise CodingWorkspaceError("workspace_base_commit_invalid")
```

已有 metadata 时，在延长 TTL 前执行：

```python
if base_commit is not None and metadata.base_commit != base_commit:
    raise CodingWorkspaceError("workspace_base_commit_mismatch")
```

- [ ] **Step 4: 增加受信 runtime metadata 字段**

在 `AssistantRuntimeFacts` 增加：

```python
repository_snapshot_sha: str | None = Field(
    default=None,
    pattern=r"^[0-9a-f]{40,64}$",
)

@model_validator(mode="after")
def _async_worker_requires_snapshot(self) -> "AssistantRuntimeFacts":
    if self.entry_profile == "async_worker" and self.repository_snapshot_sha is None:
        raise ValueError("async worker requires repository snapshot sha")
    return self
```

该字段只经 `assistant_runtime_metadata()` 写入 namespaced metadata，不加入 `AssistantRunContext` 或 Agent state。

- [ ] **Step 5: 写出 async task metadata 的失败测试**

```python
def _snapshot_sha(metadata: Mapping[str, Any]) -> str:
    return metadata[ASSISTANT_RUNTIME_METADATA_KEY]["repository_snapshot_sha"]


def test_start_keeps_creation_snapshot(monkeypatch) -> None:
    service = SimpleNamespace(repository_head=lambda repo_id: "a" * 40)
    client = _async_client()
    monkeypatch.setattr(async_delegation, "get_client", lambda **kwargs: client)
    middleware = build_async_subagent_middleware(service, "repo-sentinel")
    start = next(tool for tool in middleware.tools if tool.name == "start_async_task")
    command = asyncio.run(start.coroutine(
        description="background work",
        subagent_type=BACKGROUND_AGENT_NAME,
        runtime=_runtime(),
    ))
    task = next(iter(command.update["async_tasks"].values()))

    assert task["repository_snapshot_sha"] == "a" * 40
    assert _snapshot_sha(client.threads.create.await_args.kwargs["metadata"]) == "a" * 40
    assert _snapshot_sha(client.runs.create.await_args.kwargs["metadata"]) == "a" * 40
    assert client.runs.create.await_args.kwargs["input"] == {
        "messages": [{"role": "user", "content": "background work"}],
        "memory_context": ["memory-sentinel"],
    }
```

`_async_client()` 用 `AsyncMock` 提供 `threads.create`、`runs.create` 和 `aclose`；`_runtime()` 用 `SimpleNamespace` 提供 `state={"memory_context": ("memory-sentinel",), "async_tasks": {}}`、`config`、`tool_call_id` 与带 `StudioUser` 的 `server_info`。同文件增加 `test_update_reuses_handle_snapshot()`：把 handle 中 SHA 设为 `"a" * 40` 后调用 `update_async_task`，断言新 run metadata 仍为该 SHA；再用缺少 SHA 和 `"invalid"` 的 handle 参数化调用，断言结果以 `Failed to update async subagent snapshot:` 开头且 `client.runs.create.await_count == 0`。

- [ ] **Step 6: 在 async lifecycle 中捕获并复用 SHA**

将 builder 改为显式依赖：

```python
def build_async_subagent_middleware(
    workspace_service: CodingWorkspaceService,
    repo_id: str,
) -> AsyncSubAgentMiddleware:
```

创建 task 时只调用一次 `workspace_service.repository_head(repo_id)`，把 `tuple(runtime.state.get("memory_context") or ())` 冻结为初始 worker input，并把 SHA 同时写入 task handle 与：

```python
assistant_runtime_metadata(
    AssistantRuntimeFacts(
        entry_profile="async_worker",
        repository_snapshot_sha=repository_snapshot_sha,
    )
)
```

`_correlation_metadata()` 接收 `repository_snapshot_sha`，初始 thread、初始 run 与 `_update_async_task()` 的后续 run 都调用同一个 helper。为异步 Tool 设置 effect：`start/update/cancel=write`，`check/list=read`。

- [ ] **Step 7: 运行 Task 1 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_async_snapshot.py
```

Expected: PASS；没有网络或真实 Provider 调用。

- [ ] **Step 8: 提交 Task 1**

```bash
git add src/assistant_agent/coding/workspace.py \
  src/assistant_agent/native_agent/context.py \
  src/assistant_agent/agent_server/async_delegation.py \
  tests/tdd/unified-assistant-agent/test_async_snapshot.py
git commit -m "feat: pin async workers to repository snapshots"
```

### Task 2: 建立 worktree backend 的读写能力边界

**Files:**
- Create: `src/assistant_agent/coding/backend.py`
- Create: `tests/tdd/unified-assistant-agent/test_read_only_worker.py`
- Modify: `src/assistant_agent/native_agent/coding_agent.py`
- Modify: `src/assistant_agent/skills/native.py`

**Interfaces:**
- Consumes: Task 1 的 `AssistantRuntimeFacts.repository_snapshot_sha` 与 `CodingWorkspaceService.resolve(base_commit=明确值或None)`。
- Produces: `CodingWorkspaceBackend(SandboxBackendProtocol)`；`ReadOnlyCodingWorkspaceBackend(BackendProtocol)`；`create_project_skills_backend(project_root) -> FilesystemBackend`。

- [ ] **Step 1: 写出 backend capability 的失败测试**

```python
def test_read_only_backend_has_no_mutation_or_execute_capability() -> None:
    backend = ReadOnlyCodingWorkspaceBackend(object(), "repo-sentinel")

    assert not isinstance(backend, SandboxBackendProtocol)
    with pytest.raises(NotImplementedError):
        backend.write("/blocked.txt", "blocked")
    with pytest.raises(NotImplementedError):
        backend.edit("/blocked.txt", "a", "b")
    with pytest.raises(NotImplementedError):
        backend.delete("/blocked.txt")
    with pytest.raises(NotImplementedError):
        backend.upload_files([("/blocked.txt", b"blocked")])
    assert not hasattr(backend, "execute")
```

再写一个 runtime 测试，patch `get_runtime()` 与 `get_config()`，精确断言 metadata 到 workspace 参数的投影：

```python
@pytest.mark.parametrize("snapshot", [None, "a" * 40])
def test_backend_passes_trusted_snapshot_to_workspace(
    monkeypatch, tmp_path: Path, snapshot: str | None
) -> None:
    calls: list[dict[str, Any]] = []

    class Service:
        def resolve(self, identity, thread_id, repo_id, **kwargs):
            calls.append({
                "identity": identity,
                "thread_id": thread_id,
                "repo_id": repo_id,
                **kwargs,
            })
            return SimpleNamespace(root=tmp_path)

    facts = AssistantRuntimeFacts(repository_snapshot_sha=snapshot)
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "thread-sentinel"},
            "metadata": assistant_runtime_metadata(facts),
        },
    )

    ReadOnlyCodingWorkspaceBackend(Service(), "repo-sentinel").ls("/")

    assert calls == [{
        "identity": "user-sentinel",
        "thread_id": "thread-sentinel",
        "repo_id": "repo-sentinel",
        "base_commit": snapshot,
    }]


def test_async_worker_never_falls_back_without_snapshot(
    monkeypatch, tmp_path: Path
) -> None:
    service = SimpleNamespace(resolve=Mock())
    monkeypatch.setattr(
        backend_module,
        "get_runtime",
        lambda context_schema: SimpleNamespace(
            server_info=SimpleNamespace(user=StudioUser("user-sentinel"))
        ),
    )
    monkeypatch.setattr(
        backend_module,
        "get_config",
        lambda: {
            "configurable": {"thread_id": "worker-thread"},
            "metadata": {
                ASSISTANT_RUNTIME_METADATA_KEY: {
                    "entry_profile": "async_worker"
                }
            },
        },
    )

    with pytest.raises(CodingWorkspaceError) as exc_info:
        ReadOnlyCodingWorkspaceBackend(service, "repo-sentinel").ls("/")

    assert exc_info.value.code == "workspace_snapshot_required"
    service.resolve.assert_not_called()
```

- [ ] **Step 2: 运行测试确认只读 backend 尚不存在**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_read_only_worker.py -k backend
```

Expected: FAIL with import error for `assistant_agent.coding.backend`。

- [ ] **Step 3: 移出并收窄现有 backend**

把 `native_agent/coding_agent.py` 现有 `CodingWorkspaceBackend` 原样移动到 `coding/backend.py`，原模块暂时改为 import 该类以保持 Task 2–4 的中间提交可运行，再增加下述 snapshot 解析与最小环境限制；不要重写已有七个 delegate。Task 5 删除旧 factory 时一并删除这个临时 import。

共用解析函数必须从 runtime config 读取受信 SHA：

```python
def _workspace(
    service: CodingWorkspaceService,
    repo_id: str,
) -> CodingWorkspace:
    runtime = get_runtime(AssistantRunContext)
    config = get_config()
    thread_id = str(config.get("configurable", {}).get("thread_id", ""))
    metadata = config.get("metadata")
    raw_facts = (
        metadata.get(ASSISTANT_RUNTIME_METADATA_KEY)
        if isinstance(metadata, Mapping)
        else None
    )
    if isinstance(raw_facts, Mapping) and raw_facts.get("entry_profile") == "async_worker":
        if not raw_facts.get("repository_snapshot_sha"):
            raise CodingWorkspaceError("workspace_snapshot_required")
        try:
            facts = AssistantRuntimeFacts.model_validate(dict(raw_facts))
        except ValidationError as exc:
            raise CodingWorkspaceError("workspace_snapshot_invalid") from exc
    else:
        facts = assistant_runtime_facts(config)
    snapshot_sha = facts.repository_snapshot_sha
    return service.resolve(
        authenticated_user_identity(runtime),
        thread_id,
        repo_id,
        base_commit=snapshot_sha,
    )
```

`assistant_runtime_facts()` 对其他入口的无效 namespaced payload 仍保留现有降级语义；async worker 分支按上面代码严格解析。这样缺失 SHA 返回 `workspace_snapshot_required`，无效 SHA 返回 `workspace_snapshot_invalid`，都不会转换成 `base_commit=None`。同步 nested task 的 entry profile 不是 `async_worker`，继续复用父 thread worktree。

可写 backend 保留现有七个文件 delegate 与 `execute`，Shell backend 固定：

```python
LocalShellBackend(
    root_dir=workspace.root,
    virtual_mode=True,
    timeout=120,
    max_output_bytes=100_000,
    env={
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    },
    inherit_env=False,
)
```

只读 backend 只覆盖四个方法，delegate 使用 `FilesystemBackend(root_dir=workspace.root, virtual_mode=True)`：

```python
class ReadOnlyCodingWorkspaceBackend(BackendProtocol):
    def ls(self, path: str):
        return self._backend().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._backend().read(file_path, offset, limit)

    def grep(self, pattern: str, path: str | None = None,
             glob: str | None = None, *, max_count: int | None = None):
        return self._backend().grep(
            pattern, path=path, glob=glob, max_count=max_count
        )

    def glob(self, pattern: str, path: str | None = None):
        return self._backend().glob(pattern, path)
```

不得覆盖 `write/edit/delete/upload_files`；异步 mutation 继续走 `BackendProtocol` 对这些同步方法的 fail-closed 默认实现。不得继承 `SandboxBackendProtocol`。

- [ ] **Step 4: 将 Skills discovery backend 与模型文件视图分开**

把 `create_project_filesystem_backend()` 替换为：

```python
def create_project_skills_backend(project_root: str | Path) -> FilesystemBackend:
    return FilesystemBackend(root_dir=Path(project_root), virtual_mode=True)
```

删除 `CompositeBackend` 与 home route。保留 `create_project_filesystem_middleware()`，但后续只允许 read-only worker 使用其 `tools=["ls", "read_file", "glob", "grep"]` allowlist；主 Agent 不调用它。

- [ ] **Step 5: 运行 backend 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_read_only_worker.py -k backend
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 2**

```bash
git add src/assistant_agent/coding/backend.py \
  src/assistant_agent/native_agent/coding_agent.py \
  src/assistant_agent/skills/native.py \
  tests/tdd/unified-assistant-agent/test_read_only_worker.py
git commit -m "feat: add read-only worktree backend"
```

### Task 3: 编译单个只读 worker 并封闭 state

**Files:**
- Create: `src/assistant_agent/native_agent/assistant_agent.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/native_agent/providers.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `src/assistant_agent/coding/review.py`
- Modify: `tests/tdd/unified-assistant-agent/test_read_only_worker.py`

**Interfaces:**
- Consumes: Task 2 的 `ReadOnlyCodingWorkspaceBackend` 与 project Skills backend。
- Produces: `AssistantAgentState(DeepAgentState)`；`AssistantReadOnlyWorkerState(AgentState)`；`build_read_only_worker(model, tools, *, backend, skills_backend, tool_profiles=(), visual_history_probe=None, live_view_resolver=None, current_location=None)`；`isolated_read_only_worker(worker) -> RunnableLambda`；`read_only_worker_model_view(model)`。

- [ ] **Step 1: 写出 Worker Tool surface 的失败测试**

```python
def _tool(name: str, effect: str) -> BaseTool:
    def probe(value: str = "sentinel") -> str:
        """Return the supplied sentinel."""
        return value

    return StructuredTool.from_function(
        probe,
        name=name,
        metadata={"effect": effect},
    )


def test_worker_exposes_only_read_files_and_read_business_tools(tmp_path: Path) -> None:
    worker = build_read_only_worker(
        MockAssistantChatModel(),
        [_tool("read_probe", "read"), _tool("write_probe", "write")],
        backend=ReadOnlyCodingWorkspaceBackend(
            SimpleNamespace(), "repo-sentinel"
        ),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )
    tools = set(worker.get_graph().nodes["tools"].data.tools_by_name)

    assert {"ls", "read_file", "glob", "grep", "read_probe"} <= tools
    assert not {"write_file", "edit_file", "delete", "execute",
                "write_probe", "task", "start_async_task"} & tools
```

- [ ] **Step 2: 写出 state 双向 allowlist 的失败测试**

```python
def test_task_projection_drops_parent_and_worker_private_state() -> None:
    observed: list[dict[str, Any]] = []

    def worker(state: dict[str, Any]) -> dict[str, Any]:
        observed.append(state)
        return {
            "messages": [
                *state["messages"],
                AIMessage(content="internal-draft"),
                AIMessage(content="worker-report"),
            ],
            "provider_search_profile": "sentinel",
            "async_tasks": {"worker-task": {"status": "running"}},
            "active_tool_profile_ids": ["sentinel"],
        }

    runnable = isolated_read_only_worker(RunnableLambda(worker))
    result = runnable.invoke({
        "messages": [HumanMessage(content="task-description")],
        "memory_context": ("memory",),
        "memory_status": "ready",
        "provider_search_profile": "travel_general",
        "async_tasks": {"parent-task": {"status": "running"}},
        "active_tool_profile_ids": ["browser"],
        "future_sentinel": "private",
    })

    assert set(observed[0]) == {"messages", "memory_context"}
    assert set(result) == {"messages"}
    assert [message.content for message in result["messages"]] == ["worker-report"]


def test_task_projection_returns_only_nonempty_structured_response() -> None:
    runnable = isolated_read_only_worker(RunnableLambda(lambda state: {
        "messages": [AIMessage(content="worker-report")],
        "structured_response": {"answer": "sentinel"},
        "async_tasks": {"blocked": {}},
    }))

    result = runnable.invoke({
        "messages": [HumanMessage(content="task-description")],
        "memory_context": (),
    })

    assert set(result) == {"messages", "structured_response"}
    assert result["structured_response"] == {"answer": "sentinel"}
```

- [ ] **Step 3: 运行测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_read_only_worker.py
```

Expected: FAIL；新 state 与 worker builder 尚未定义。

- [ ] **Step 4: 收窄 state schema**

在 `state.py` 定义：

```python
class AssistantRootState(MessagesState):
    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    async_tasks: NotRequired[AsyncTasks]


class AssistantAgentState(DeepAgentState):
    memory_context: NotRequired[tuple[str, ...]]
    memory_status: NotRequired[MemoryStatus]
    provider_search_profile: NotRequired[ProviderSearchProfile]
    async_tasks: NotRequired[AsyncTasks]


class AssistantReadOnlyWorkerState(AgentState):
    memory_context: NotRequired[tuple[str, ...]]
```

删除 `ExecutionMode`、`FastAgentState`、`PlanningAgentState`、`CodingState`、`CodingAnalysisWorkerState`。`merge_attestation_mismatch_signals` 仍被 `coding/review.py` 的两个 schema 使用，把函数和 allowlist 原样移到该模块后再从 `state.py` 删除，避免保留 coding domain 对已退役 graph state 的反向依赖。

- [ ] **Step 5: 从现有 fast agent 迁移共用 middleware**

用 `apply_patch` 把以下实现原样移入 `assistant_agent.py`，此步不改其逻辑；`fast_agent.py` 在 Task 3–4 只从新模块 import 这些名称，避免中间提交复制实现，Task 5 再删除旧模块：

- `RecursionFinalSynthesisState`
- `RecursionFinalSynthesisMiddleware`
- `MemoryContextMiddleware`
- `ToolProgressMiddleware`
- `_tool_progress_event()`
- `_retryable_read_tool_names()`
- `_request_with_memory_context()`
- `memory_context_message()`

保留 `step_reserve=8`、同一 superstep 同名 Tool 最多 12 个、read retry 两次、75% summarization trigger 与 15% keep。

- [ ] **Step 6: 实现 worker 与正向投影**

Worker 使用 `create_agent`，因为它必须完全不注册 Deep Agents 自动 `task`；主生产 Agent 仍是唯一 `create_deep_agent` 主循环。middleware 顺序固定为 core prompt、Skills、只读 Filesystem、条件 Tool 暴露、per-tool limit、summarization、Memory、runtime prompt、final synthesis、progress、read retry。

```python
def _worker_input(state: Mapping[str, Any]) -> dict[str, Any]:
    messages = list(state.get("messages") or ())
    if len(messages) != 1 or not isinstance(messages[0], HumanMessage):
        raise ValueError("task worker requires exactly one task description")
    result: dict[str, Any] = {"messages": [messages[0]]}
    if "memory_context" in state:
        result["memory_context"] = tuple(state["memory_context"])
    return result


def _worker_output(result: Mapping[str, Any]) -> dict[str, Any]:
    final_message = next(
        (
            message
            for message in reversed(result.get("messages") or ())
            if isinstance(message, AIMessage) and message.text.strip()
        ),
        AIMessage(content=""),
    )
    output = {"messages": [final_message]}
    if result.get("structured_response") is not None:
        output["structured_response"] = result["structured_response"]
    return output


def isolated_read_only_worker(worker: Runnable) -> RunnableLambda:
    def invoke(state: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
        return _worker_output(worker.invoke(_worker_input(state), config))

    async def ainvoke(
        state: Mapping[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        return _worker_output(await worker.ainvoke(_worker_input(state), config))

    return RunnableLambda(invoke, afunc=ainvoke)
```

`build_read_only_worker()` 只把 `(tool.metadata or {}).get("effect") == "read"` 的业务 Tool 传给 `create_agent`，state schema 固定为 `AssistantReadOnlyWorkerState`，name 固定为 `AssistantReadOnlyWorker`。

- [ ] **Step 7: 重命名 worker model view 并删除 mock 强制 planning**

把 `planning_supervisor_model_view()` 改名为 `read_only_worker_model_view()`，语义仍为关闭 Qwen provider-native search。删除 `_mock_planning_agent_response()` 及 `_response_message()` 对它的调用；默认 mock 对简单请求直接返回标准 `AIMessage`。需要触发 `task` 的测试继续使用显式 scripted model。

- [ ] **Step 8: 运行 Worker 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_read_only_worker.py
```

Expected: PASS。

- [ ] **Step 9: 提交 Task 3**

```bash
git add src/assistant_agent/native_agent/assistant_agent.py \
  src/assistant_agent/native_agent/state.py \
  src/assistant_agent/native_agent/providers.py \
  src/assistant_agent/native_agent/fast_agent.py \
  src/assistant_agent/coding/review.py \
  tests/tdd/unified-assistant-agent/test_read_only_worker.py
git commit -m "feat: compile isolated read-only assistant worker"
```

### Task 4: 构造统一 `AssistantAgent` 与副作用 HITL

**Files:**
- Create: `tests/tdd/unified-assistant-agent/test_unified_graph.py`
- Modify: `src/assistant_agent/native_agent/assistant_agent.py`
- Modify: `src/assistant_agent/native_agent/tool_profiles.py`
- Modify: `src/assistant_agent/agent_server/services.py`

**Interfaces:**
- Consumes: Task 3 的 `build_read_only_worker()` 和 `isolated_read_only_worker()`；Task 2 的 `CodingWorkspaceBackend`。
- Produces: `build_assistant_agent(model, tools, *, backend, worker_graph, skills_backend, tool_profiles=(), additional_middleware=(), visual_history_probe=None, live_view_resolver=None, current_location=None, checkpointer=None)`；唯一进程级 main graph 与 worker graph。

- [ ] **Step 1: 写出 main factory 使用原生 FS middleware 的失败测试**

```python
def test_main_uses_factory_filesystem_and_unified_hitl(monkeypatch, tmp_path) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        assistant_agent,
        "create_deep_agent",
        lambda **kwargs: captured.update(kwargs) or "compiled",
    )
    result = build_assistant_agent(
        MockAssistantChatModel(),
        [_tool("read_probe", "read"), _tool("write_probe", "write")],
        backend=object(),
        worker_graph=RunnableLambda(lambda state: state),
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
        additional_middleware=(SimpleNamespace(tools=[
            _tool("start_async_task", "write"),
            _tool("check_async_task", "read"),
        ]),),
    )

    assert result == "compiled"
    assert not any(isinstance(item, FilesystemMiddleware)
                   for item in captured["middleware"])
    assert captured["interrupt_on"].keys() >= {
        "write_file", "edit_file", "delete", "execute", "write_probe",
        "start_async_task",
    }
    assert "check_async_task" not in captured["interrupt_on"]
    assert [item["name"] for item in captured["subagents"]] == ["general-purpose"]
    assert captured["name"] == "AssistantAgent"
```

- [ ] **Step 2: 写出简单直答与副作用审批测试**

```python
class _WriteOnceModel(MockAssistantChatModel):
    def _response_message(self, messages, **kwargs):
        del kwargs
        if any(
            isinstance(message, ToolMessage) and message.name == "write_probe"
            for message in messages
        ):
            return AIMessage(content="write-complete")
        return AIMessage(content="", tool_calls=[{
            "name": "write_probe",
            "args": {"value": "sentinel"},
            "id": "write-call",
            "type": "tool_call",
        }])


def _compiled_agent(tmp_path: Path, model: BaseChatModel,
                    tools: Sequence[BaseTool] = ()):
    read_only_backend = ReadOnlyCodingWorkspaceBackend(
        SimpleNamespace(), "repo-sentinel"
    )
    worker = build_read_only_worker(
        read_only_worker_model_view(model),
        tools,
        backend=read_only_backend,
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )
    return build_assistant_agent(
        model,
        tools,
        backend=LocalShellBackend(root_dir=tmp_path, virtual_mode=True),
        worker_graph=worker,
        skills_backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
        tool_profiles=(),
    )


def test_simple_request_does_not_require_todo_or_task(tmp_path: Path) -> None:
    graph = _compiled_agent(tmp_path, MockAssistantChatModel())
    result = graph.invoke(
        {"messages": [HumanMessage(content="simple-sentinel")]},
        context=AssistantRunContext(),
        config={"configurable": {"thread_id": "simple-thread"}},
    )
    assert isinstance(result["messages"][-1], AIMessage)
    assert not any(isinstance(message, ToolMessage) for message in result["messages"])


@pytest.mark.parametrize("decision,expected", [("approve", 1), ("reject", 0)])
def test_write_interrupts_before_handler_and_resumes_once(
    tmp_path: Path, decision: str, expected: int
) -> None:
    executed: list[str] = []

    def write_probe(value: str) -> str:
        """Record one governed write."""
        executed.append(value)
        return "ok"

    tool = StructuredTool.from_function(
        write_probe, name="write_probe", metadata={"effect": "write"}
    )
    assistant = _compiled_agent(tmp_path, _WriteOnceModel(), [tool])
    builder = StateGraph(AssistantAgentState, context_schema=AssistantRunContext)
    builder.add_node("assistant", assistant)
    builder.add_edge(START, "assistant")
    builder.add_edge("assistant", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"write-{decision}"}}

    interrupted = graph.invoke(
        {"messages": [HumanMessage(content="write sentinel")]},
        context=AssistantRunContext(),
        config=config,
    )
    assert executed == []
    assert interrupted["__interrupt__"][0].value["action_requests"][0]["name"] == "write_probe"

    graph.invoke(
        Command(resume={"decisions": [{"type": decision}]}),
        context=AssistantRunContext(),
        config=config,
    )
    assert len(executed) == expected
```

现有 `CTX-001` 的 resume/replay 测试同步迁移到 unified graph，并保留“同一 tool call id 最多执行一次”的断言；不要另造第二套幂等实现。

- [ ] **Step 3: 运行 unified Agent 测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_unified_graph.py -k "main or simple or write"
```

Expected: FAIL because `build_assistant_agent()` 尚未实现。

- [ ] **Step 4: 定义统一审批策略**

```python
_APPROVAL = {"allowed_decisions": ["approve", "edit", "reject"]}
_FILESYSTEM_SIDE_EFFECTS = ("write_file", "edit_file", "delete", "execute")


def _interrupt_on(tools: Sequence[BaseTool]) -> dict[str, object]:
    result = {name: _APPROVAL for name in _FILESYSTEM_SIDE_EFFECTS}
    for tool in tools:
        metadata = tool.metadata or {}
        effect = metadata.get("effect")
        if effect in {"write", "dangerous", "generate"} or (
            metadata.get("source") == "mcp" and effect != "read"
        ):
            result[tool.name] = _APPROVAL
    return result
```

`build_assistant_agent()` 先把 `additional_middleware` 中公开的 `.tools` 展平成 `middleware_tools`，再调用 `_interrupt_on([*tools, *middleware_tools])`。这样 `start_async_task`、`update_async_task`、`cancel_async_task` 也进入审批，而 `check/list` 保持只读。不得依据用户文本、Prompt、Skill 或 mode 决定审批。

- [ ] **Step 5: 实现唯一 `build_assistant_agent()`**

调用 `create_deep_agent` 时使用：

```python
middleware_tools = tuple(
    tool
    for item in additional_middleware
    for tool in getattr(item, "tools", ())
)
read_tool_names = tuple(sorted({
    "ls", "read_file", "glob", "grep",
    *(
        tool.name
        for tool in (*tools, *middleware_tools)
        if (tool.metadata or {}).get("effect") == "read"
    ),
}))

return create_deep_agent(
    model=model,
    tools=list(tools),
    backend=backend,
    subagents=[{
        "name": "general-purpose",
        "description": _GENERAL_PURPOSE_DESCRIPTION_ZH,
        "runnable": isolated_read_only_worker(worker_graph),
    }],
    state_schema=AssistantAgentState,
    context_schema=AssistantRunContext,
    middleware=[
        create_assistant_base_prompt(),
        create_project_skills_middleware(skills_backend),
        ToolProfileMiddleware(tool_profiles),
        ConditionalToolExposureMiddleware(visual_history_probe, live_view_resolver),
        TodoListMiddleware(
            system_prompt=_WRITE_TODOS_SYSTEM_PROMPT_ZH,
            tool_description=_WRITE_TODOS_DESCRIPTION_ZH,
        ),
        *additional_middleware,
        PerToolCallLimitMiddleware(max_parallel_calls_per_tool=12),
        SummarizationMiddleware(**summarization_options),
        MemoryContextMiddleware(),
        create_assistant_runtime_prompt(current_location),
        RecursionFinalSynthesisMiddleware(),
        ToolProgressMiddleware(),
        *([ToolRetryMiddleware(
            max_retries=2,
            tools=read_tool_names,
            initial_delay=0,
            backoff_factor=0,
            jitter=False,
        )] if read_tool_names else []),
    ],
    interrupt_on=_interrupt_on([*tools, *middleware_tools]),
    name="AssistantAgent",
)
```

不要传 `skills=`，否则上游会让 Skills discovery 访问 main worktree；不要加入自定义 `FilesystemMiddleware`，由 `create_deep_agent` 自己生成。`TodoListMiddleware` 只能出现一次。

- [ ] **Step 6: 把 `execute` 纳入 filesystem Profile**

在 `project_tool_profiles()` 的 `filesystem.tool_names` 末尾加入 `"execute"`。异步 Tool Profile 继续独立；`task` 与 `write_todos` 不归入任何 Profile，保持核心可见。

- [ ] **Step 7: 简化 process composition**

`AgentServerExecutionOwner.compose()` 的顺序固定为：

```text
Provider/Tool/Memory config
  -> CodingWorkspaceService
  -> project Skills backend
  -> ReadOnlyCodingWorkspaceBackend
  -> AssistantReadOnlyWorker
  -> authenticated AsyncSubAgentMiddleware(workspace service, repo id)
  -> CodingWorkspaceBackend
  -> AssistantAgent
  -> AssistantRootGraph
  -> Memory graph
```

只构造一次 worker，并同时作为同步 CompiledSubAgent runnable 与 `owner.worker_graph`。删除 fast/planning/coding 三套 option dict 和重复 factory 调用。

- [ ] **Step 8: 运行 Task 4 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_unified_graph.py
```

Expected: PASS。

- [ ] **Step 9: 提交 Task 4**

```bash
git add src/assistant_agent/native_agent/assistant_agent.py \
  src/assistant_agent/native_agent/tool_profiles.py \
  src/assistant_agent/agent_server/services.py \
  tests/tdd/unified-assistant-agent/test_unified_graph.py
git commit -m "feat: unify assistant planning and execution"
```

### Task 5: 切换父图并删除三模式生产代码

**Files:**
- Modify: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Delete: `src/assistant_agent/native_agent/fast_agent.py`
- Delete: `src/assistant_agent/native_agent/planning_agent.py`
- Delete: `src/assistant_agent/native_agent/coding_agent.py`
- Delete: `src/assistant_agent/native_agent/coding_graph.py`
- Modify: `tests/tdd/unified-assistant-agent/test_unified_graph.py`

**Interfaces:**
- Consumes: Task 4 的已编译 `AssistantAgent`。
- Produces: `build_assistant_root_graph(*, memory_backend, assistant_agent, extraction_delay_seconds=DEFAULT_EXTRACTION_DELAY_SECONDS)`；唯一 `memory_recall -> assistant_agent -> refresh_memory_extraction` 路线。

- [ ] **Step 1: 写出父图拓扑失败测试**

```python
def test_parent_graph_has_one_execution_route() -> None:
    graph = build_assistant_root_graph(
        memory_backend=_Memory(),
        assistant_agent=RunnableLambda(lambda state: {"messages": state["messages"]}),
        extraction_delay_seconds=0,
    )
    nodes = set(graph.get_graph().nodes)

    assert {"memory_recall", "assistant_agent", "refresh_memory_extraction"} <= nodes
    assert not {"execution_router", "fast_agent", "planning_agent", "coding_agent"} & nodes
```

- [ ] **Step 2: 运行拓扑测试确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_unified_graph.py -k parent_graph
```

Expected: FAIL because root builder still requires three agents。

- [ ] **Step 3: 删除 execution router**

Root graph 精确改成：

```python
builder.add_node(
    "memory_recall",
    partial(memory_recall_node, backend=memory_backend),
    retry_policy=RetryPolicy(
        initial_interval=0,
        backoff_factor=0,
        max_attempts=3,
        jitter=False,
    ),
)
builder.add_node("assistant_agent", assistant_agent)
builder.add_node(
    "refresh_memory_extraction",
    partial(
        refresh_memory_extraction_node,
        delay_seconds=extraction_delay_seconds,
        enabled=memory_backend.backend_id != "disabled",
    ),
    retry_policy=RetryPolicy(
        initial_interval=0,
        backoff_factor=0,
        max_attempts=3,
        jitter=False,
    ),
)
builder.add_edge(START, "memory_recall")
builder.add_edge("memory_recall", "assistant_agent")
builder.add_edge("assistant_agent", "refresh_memory_extraction")
builder.add_edge("refresh_memory_extraction", END)
```

删除 `execution_router_node()`、`route_execution_mode()` 及其 export。Memory recall、retry、rollback/enqueue 逻辑不改。

- [ ] **Step 4: 删除旧 factory 与未引用 coding graph**

先运行：

```bash
rg -n "build_fast_agent|build_planning_agent|build_coding_agent|build_coding_graph" src tests/core scripts evals
```

把生产和 core 调用方迁到 `build_assistant_agent()` 后，使用 `apply_patch` 删除四个旧模块。不得保留 alias、空 factory 或 mode adapter；`coding/workspace.py`、`coding/backend.py` 和仍被 workspace/eval-independent 代码引用的 coding model/service 保留。

- [ ] **Step 5: 运行 TDD 组合测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 5**

```bash
git add src/assistant_agent/native_agent/root_graph.py \
  src/assistant_agent/native_agent/state.py \
  src/assistant_agent/agent_server/services.py \
  src/assistant_agent/native_agent/fast_agent.py \
  src/assistant_agent/native_agent/planning_agent.py \
  src/assistant_agent/native_agent/coding_agent.py \
  src/assistant_agent/native_agent/coding_graph.py \
  tests/tdd/unified-assistant-agent/test_unified_graph.py
git commit -m "refactor: collapse assistant root graph"
```

### Task 6: 升级 graph identity 并删除所有客户端 mode

**Files:**
- Create: `tests/tdd/unified-assistant-agent/test_client_contract.py`
- Modify: `src/assistant_agent/agent_server/config.py`
- Modify: `src/assistant_agent/agent_server/auth.py`
- Modify: `src/assistant_agent/agent_server/graph.py`
- Modify: `src/assistant_agent/agent_server/media_protocol.py`
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `src/assistant_agent/evaluation/native_graph_target.py`
- Modify: `evals/system/tools/native_tool.py`
- Modify: `scripts/agent_cli.py`
- Modify: `scripts/media_simulator.py`
- Modify: `langgraph.json`
- Delete: `src/assistant_agent/agent_server/attestation.py`
- Delete: `src/assistant_agent/evaluation/coding_agent_server.py`
- Delete: `src/assistant_agent/evaluation/coding_behavior.py`
- Delete: `evals/system/ai_coding_behavior/`
- Delete: `scripts/run_system_ai_coding_behavior_eval.py`

**Interfaces:**
- Consumes: Task 5 的唯一 graph 与公开 `AssistantRunContext(enable_memory=True)`。
- Produces: `ASSISTANT_GRAPH_ID="assistant-native-v4"`、`WORKER_GRAPH_ID="assistant-worker-v2"`；无 mode 的 Media/CLI/evaluation 请求。

- [ ] **Step 1: 写出 mode 删除和 v4/v2 的失败测试**

```python
def test_graph_ids_and_public_context_are_v4_only() -> None:
    assert ASSISTANT_GRAPH_ID == "assistant-native-v4"
    assert WORKER_GRAPH_ID == "assistant-worker-v2"
    assert set(AssistantRunContext.model_fields) == {"enable_memory"}
    with pytest.raises(ValidationError):
        AssistantRunContext.model_validate({"execution_mode": "fast"})


def test_media_rejects_removed_assistant_mode() -> None:
    envelope = _chat_envelope()
    envelope.body["assistantMode"] = "planning"
    with pytest.raises(MediaProtocolError, match="assistantMode is not supported"):
        parse_chat(envelope)


def test_simulator_chat_body_has_no_assistant_mode() -> None:
    assert "assistantMode" not in chat_body(
        text="hello", chat_index="1", user_number="u", speaker_number="s",
        stream=True,
    )


def test_media_stream_keeps_main_model_and_hides_worker_model() -> None:
    stream = _NativeAssistantTextStream()
    stream._record_metadata({
        "main-message": {"metadata": {
            "langgraph_node": "model",
            "lc_agent_name": "AssistantAgent",
        }},
        "worker-message": {"metadata": {
            "langgraph_node": "model",
            "lc_agent_name": "general-purpose",
        }},
    })

    assert stream.message_nodes == {
        "main-message": "model",
        "worker-message": "__internal_subgraph__",
    }


def test_obsolete_coding_attestation_route_is_removed() -> None:
    assert "/internal/evaluation/coding-attestation" not in {
        route.path for route in app.routes
    }
```

- [ ] **Step 2: 运行 client tests 确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_client_contract.py
```

Expected: FAIL on old graph IDs、context field 和 `assistantMode`。

- [ ] **Step 3: 升级 graph IDs，不保留 alias**

`config.py` 与 `langgraph.json` 只注册：

```text
assistant-native-v4 -> native_assistant_graph
assistant-worker-v2 -> native_worker_graph
assistant-memory-v1 -> native_memory_graph
```

`AgentServerExecutionAttestation` 及其 loopback coding eval endpoint 只服务已失效旧 runner，直接删除；同步删除 auth 中 `coding_eval_*` token 签发/验证分支和 graph getter。正常 owner + graph identity auth 不变。

同时删除 `AgentServerExecutionOwner.execution_attestation`、compose 中的 attestation 构造，以及 `media_app.py` 的 `/internal/evaluation/coding-attestation` route/import；这些入口没有 unified state 的可验证语义，不能只改名保留。

Worker graph 的创建权限同时 fail closed：`authorize_thread_create()` 遇到 `WORKER_GRAPH_ID`，以及 `authorize_run_create()` 遇到 worker assistant 时，都必须从 namespaced metadata 严格解析 `AssistantRuntimeFacts`，并要求 `entry_profile == "async_worker"` 且 `repository_snapshot_sha` 存在。缺失、非法或由宽松 helper 降级的 payload 返回 `False`。gateway test 分别覆盖缺失 SHA 被拒绝、合法完整 SHA 被允许；这样直接调用 worker-v2 也不能绕过 async lifecycle 回退当前 HEAD。

- [ ] **Step 4: 从公开 context 删除 mode**

`AssistantRunContext` 精确保留：

```python
class AssistantRunContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    enable_memory: bool = Field(
        default=True,
        json_schema_extra={
            "langgraph_nodes": ["memory_recall", "refresh_memory_extraction"]
        },
    )
```

- [ ] **Step 5: 删除 Media `assistantMode`**

从 `MediaChat` 删除 `execution_mode`。`parse_chat()` 在读取 contents 前显式拒绝：

```python
if "assistantMode" in body:
    raise MediaProtocolError("assistantMode is not supported")
```

`media_app.py` 创建 run 时改用 `context={"enable_memory": True}`，保留现有 `assistant_runtime_metadata()` 和视觉 metadata，不推断 mode。

同时把 `_NativeAssistantTextStream` 对旧 `fast_agent:` checkpoint namespace 的判断替换为受信 tracing metadata：`lc_agent_name == "AssistantAgent"` 是唯一可直接流向媒体的 model，`general-purpose` 和 `AssistantReadOnlyWorker` model 都标为 `__internal_subgraph__`。保留无该 metadata 的顶层 `node == "model"` 兼容 Agent Server 本身的事件，不根据文本内容判断。

- [ ] **Step 6: 删除 CLI 与 simulator mode**

`agent_cli._run_once()` 删除 `mode` 参数，input 只含 messages；parser 删除 `--assistant-mode`，交互命令删除 `/fast`、`/planning`。CLI 的入口事实改放：

```python
metadata=assistant_runtime_metadata(AssistantRuntimeFacts(entry_profile="cli"))
```

`media_simulator.chat_body()` 删除 `assistant_mode` 参数和 `assistantMode` 字段；删除 mode 局部变量、`/fast|/planning` 分支和帮助文本。

- [ ] **Step 7: 收窄 evaluation target 并退休旧 CodingGraph runner**

`NativeGraphEvaluationTarget.ainvoke()` 删除 `execution_mode` 参数，固定 `context=AssistantRunContext()`。删除绑定 `coding_result`、patch/review/merge interrupt 与旧 graph attestation 的 runner 文件；不得把 mock fallback 或旧 state projection 改名后保留。

`evals/system/tools/native_tool.py` 的孤立 ToolNode harness 改用 `AgentState`，默认 `AssistantRunContext()`；`entry_profile="system_eval"` 改写到 `assistant_runtime_metadata(AssistantRuntimeFacts(...))`，不经过统一 Agent loop，也不保留 `FastAgentState`。

- [ ] **Step 8: 运行 client tests 与 CLI help**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent/test_client_contract.py
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/agent_cli.py --help
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/media_simulator.py --help
```

Expected: PASS；help 中不存在 assistant mode、fast 或 planning 选项。

- [ ] **Step 9: 提交 Task 6**

```bash
git add langgraph.json src/assistant_agent/agent_server \
  src/assistant_agent/evaluation evals/system/tools/native_tool.py \
  scripts/agent_cli.py scripts/media_simulator.py \
  scripts/run_system_ai_coding_behavior_eval.py evals/system/ai_coding_behavior \
  tests/tdd/unified-assistant-agent/test_client_contract.py
git commit -m "refactor: remove assistant execution modes"
```

### Task 7: 更新既有 core invariant 测试

**Files:**
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Modify: `tests/core/contract/test_tool_contract.py`
- Modify: `tests/core/contract/test_gateway_contract.py`
- Modify: `tests/core/contract/test_observability_contract.py`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: Tasks 1–6 的最终生产 API。
- Produces: 更新后的 `LOOP-001`、`CTX-001`、`MEMORY-001`、`GATE-001`、`IDENT-001` 永久安全网；不增加新 invariant ID 或新 core 文件。

- [ ] **Step 1: 重写 `LOOP-001` 测试**

删除 mode 参数化和三 factory 断言，保留以下结构化测试：

```python
@pytest.mark.core_invariant("LOOP-001")
def test_parent_graph_has_one_native_deep_agent_route(monkeypatch) -> None:
    owner = asyncio.run(_open_owner())
    try:
        nodes = set(owner.graph.get_graph().nodes)
        assert {"memory_recall", "assistant_agent", "refresh_memory_extraction"} <= nodes
        assert not {"execution_router", "fast_agent", "planning_agent", "coding_agent"} & nodes
        assistant = owner.graph.get_graph().nodes["assistant_agent"].data
        tools = set(assistant.get_graph().nodes["tools"].data.tools_by_name)
        assert {"write_todos", "task", "ls", "read_file", "write_file",
                "edit_file", "delete", "glob", "grep", "execute"} <= tools
    finally:
        asyncio.run(owner.aclose())
```

继续断言无 model/run 累计 limit、per-tool parallel limit=12、remaining steps final synthesis=8、标准最终 `AIMessage`。

- [ ] **Step 2: 重写 `CTX-001` 测试**

保留 Memory 临时投影、summary、retry、Tool Profile、条件暴露和 final synthesis 测试；删除 fast 自动放行断言，替换为统一副作用中断。同步 task 测试必须断言：

```python
assert set(worker_state) == {"messages", "memory_context"}
assert "provider_search_profile" not in worker_state
assert "async_tasks" not in worker_state
assert result["async_tasks"] == parent_tasks
assert "active_tool_profile_ids" not in result
```

在同一测试函数中加入：

```python
worker_tools = set(owner.worker_graph.get_graph().nodes["tools"].data.tools_by_name)
assert not {
    "write_file", "edit_file", "delete", "execute", "task", "start_async_task"
} & worker_tools
read_only_backend = ReadOnlyCodingWorkspaceBackend(
    SimpleNamespace(), "repo-sentinel"
)
with pytest.raises(NotImplementedError):
    read_only_backend.write("/blocked.txt", "blocked")
```

- [ ] **Step 3: 更新 Memory lifecycle**

把“每个 mode recall”参数化改成单一 unified run；`enable_memory=False`、三次 RetryPolicy、rollback/enqueue 语义不改。旧 `test_planning_task_preserves_parent_memory_status` 改为“worker 看不到 memory_status，parent memory_status 保持 ready”。

- [ ] **Step 4: 更新 Tool、observability 与 gateway tests**

- `test_tool_contract.py`、`test_observability_contract.py` 改从 `assistant_agent.native_agent.assistant_agent` 导入统一 builder。
- gateway 只接受 `assistant-native-v4`、`assistant-worker-v2`、`assistant-memory-v1`，拒绝 v1/v2/v3 与 unknown checkpoint/thread。
- Media parser 的合法 chat fixture 删除 `assistantMode`，并增加旧字段拒绝断言。
- IDENT 测试断言 `AssistantRunContext.model_fields == {"enable_memory"}`，入口/媒体事实仍只在 namespaced metadata。

- [ ] **Step 5: 重写 invariant prose**

`tests/core/INVARIANTS.md` 精确表达：

- `LOOP-001`：唯一静态父图和唯一 `AssistantAgent`；Deep Agents 官方 loop；主写/worker 只读；async 创建时 SHA。
- `CTX-001`：统一 Prompt/Memory/Todo/task/summary/retry/HITL；task 输入输出 allowlist；全部副作用审批。
- `GATE-001`：v4/v2/v1 三个 graph identity，无旧 alias。
- `IDENT-001`：公开 context 只有 `enable_memory`，identity/entry/media/SHA 只走 server/runtime metadata。

- [ ] **Step 6: 运行 core 测试并修复真实回归**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_memory_lifecycle.py \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_gateway_contract.py \
  tests/core/contract/test_observability_contract.py
```

Expected: PASS；不得通过放宽 invariant 或跳过测试解决失败。

- [ ] **Step 7: 提交 Task 7**

```bash
git add tests/core/INVARIANTS.md tests/core/integration \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_gateway_contract.py \
  tests/core/contract/test_observability_contract.py
git commit -m "test: protect unified assistant invariants"
```

### Task 8: 同步 authority、验证热重载并完成迁移

**Files:**
- Modify: `README.md`
- Modify: `docs/authority.toml`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/visual-perception-architecture.md`
- Modify: `evals/README.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: Tasks 1–7 的真实源码和测试结果。
- Produces: 与源码一致的 owner authority、机器路由和最终验证证据。

- [ ] **Step 1: 更新文档路由**

`docs/authority.toml`：

- runtime-event-stream source globs 用 `native_agent/assistant_agent.py` 和 `coding/backend.py` 替换 fast/planning/coding agent；
- context-engineering source globs 用 `assistant_agent.py` 替换 fast/planning；
- system-eval 删除 `run_system_ai_coding_behavior_eval.py` source 与 dry-run verification；
- verification 保留现有 mock core 命令并加入 `tests/tdd/unified-assistant-agent` 的显式命令。

- [ ] **Step 2: 更新 runtime/context/tool authority**

文档必须写明以下事实，不保留三模式现在时描述：

```text
AssistantRootGraph:
  memory_recall -> AssistantAgent -> refresh_memory_extraction

AssistantAgent:
  direct answer | write_todos | task(read-only) | tools | worktree FS | execute
```

Tool authority 记录 main factory-owned `FilesystemMiddleware`、worker read allowlist + read-only backend、`#5388` 回归测试、统一 HITL。Context authority 记录公开 context 只有 `enable_memory`、task state 双向 allowlist、Skills discovery backend 与模型 worktree backend 分离。

- [ ] **Step 3: 更新 Agent Server、Media、visual 与 eval authority**

- Agent Server：v4/v2/v1 identity、创建时 snapshot SHA、旧 v3/worker-v1 不 resume/replay。
- Media：wire 不再发送或接受 `assistantMode`，入口只投影标准 messages 和受信 metadata。
- Visual：只把“fast/planning 路由”措辞替换为统一 Agent；不得修改、串行化或删除现有并行视觉流水线。
- Eval：删除旧 CodingGraph behavior baseline，保留 `NativeGraphEvaluationTarget` 和真实 Provider 门禁；明确未来 unified coding behavior eval 需另立当前 state/interrupt 契约。
- Scripts/README 与根 README 删除 mode 和旧 runner 导航。

- [ ] **Step 4: 运行全量离线验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/tdd/unified-assistant-agent
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  -m pytest -q tests/core
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: 全部 exit 0；无网络与真实 Provider 调用。

- [ ] **Step 5: 扫描残留旧契约**

Run:

```bash
rg -n "assistantMode|\\bexecution_mode\\b|\\bExecutionMode\\b|assistant-native-v3|assistant-worker-v1|build_fast_agent|build_planning_agent|build_coding_agent" \
  src tests/core scripts evals langgraph.json README.md docs/*.md
```

Expected: 无生产、core、当前 authority 命中；只允许已批准且未跟踪的历史 design/spec 提及迁移前名称。

- [ ] **Step 6: 验证现有 8089 Agent Server hot reload**

不得启动第二个 dev server。等待 PyCharm 管理的 `8089` 完成 reload，然后运行：

```bash
curl -fsS -H 'X-Assistant-User: local-developer' \
  http://127.0.0.1:8089/assistants/assistant-native-v4/graph >/dev/null
```

Expected: exit 0。若 8089 本轮未运行，报告“未验证现有服务 reload”，不要另起并行实例。

- [ ] **Step 7: 检查提交范围并提交 authority**

```bash
git status --short
git diff --check
git add README.md docs/authority.toml docs/*.md evals/README.md scripts/README.md
git commit -m "docs: describe unified assistant runtime"
```

不得 add `docs/superpowers/specs/2026-08-27-unified-assistant-agent-design.md` 或本计划。

- [ ] **Step 8: 最终报告**

按仓库格式报告：完成内容；`Core invariant: LOOP-001, CTX-001, MEMORY-001, GATE-001, IDENT-001`；`Tests:` 后列实际命令与结果；8089 reload 状态；未调用真实 Provider；旧 checkpoint 不兼容；临时 `tests/tdd/unified-assistant-agent/` 保留供用户自行删除。
