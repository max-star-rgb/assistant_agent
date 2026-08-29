# Thread Artifact 与 Stateful MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 Agent thread 提供独立的 Git worktree、非 Git artifact 目录和 Stateful Playwright MCP session，使浏览器状态跨 run 连续，文件型输出可读且永不进入 Git。

**Architecture:** 复用 `ThreadWorktreeManager` 的管理根，在 `repo/` 旁增加 `artifacts/`；Deep Agents 原生 `CompositeBackend` 保持 repo 为默认根，把 `/artifacts/` 路由到 sibling 目录。MCP Tool 继续由官方 adapter discovery/convert，仅 `session_scope="thread"` 的调用经官方 interceptor 转发到 thread-keyed `ClientSession` pool；图片生成、HTTP、媒体和 3D 消费统一解析 thread artifact 引用。

**Tech Stack:** Python 3.12、LangGraph/Deep Agents、LangChain MCP Adapters 0.3.2、MCP Python SDK、Playwright MCP 0.0.78、Pydantic、pytest。

**Spec:** `docs/superpowers/specs/2026-08-28-thread-artifact-stateful-mcp-design.md`

## Global Constraints

- 默认 mock/offline，不调用真实模型或真实 Provider。
- Git worktree 虚拟根保持 `/`；源码路径和 shell cwd 不增加 `/repo/` 前缀。
- artifact 只存在于 `<workspace_ref>/artifacts/`，不进入 Git status、diff、patch、commit 或回灌。
- Stateful MCP key 包含受信 identity、thread、repo、server；不同 thread 不共享浏览器状态。
- 未声明 `session_scope="thread"` 的 MCP 保持官方 stateless 行为。
- 不复制 MCP Tool schema，不实现 MCP proxy，不新增私有 MCP 文件协议。
- workspace/artifact 使用同一 TTL；清理顺序固定为关闭 session、移除 Git worktree、删除管理根。
- 保留 Playwright MCP `0.0.78`，不顺带升级依赖。
- 工作区已有未提交的 native-worktree-delivery 改动；不得回滚、覆盖或夹带提交。
- Core invariant 不变；仅新增临时 `tests/tdd/thread-artifact-stateful-mcp/`。

---

### Task 1: 增加 sibling artifact root 与组合 backend

**Files:**
- Modify: `src/assistant_agent/worktree/manager.py`
- Modify: `src/assistant_agent/worktree/backend.py`
- Modify: `src/assistant_agent/worktree/__init__.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Create: `tests/tdd/thread-artifact-stateful-mcp/test_worktree_artifacts.py`

**Interfaces:**
- Produces: `ThreadWorktree.artifact_root: Path`。
- Produces: `ThreadWorktreeManager.resolve_artifact_root(workspace_ref: str) -> Path`。
- Produces: `ThreadArtifactBackend` 与 `create_thread_workspace_backend(manager: ThreadWorktreeManager, repo_id: str) -> CompositeBackend`。

- [ ] **Step 1: 写 artifact/Git 隔离 RED 测试**

```python
def test_artifact_root_is_a_non_git_sibling(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    manager = manager_for(tmp_path, repo)
    worktree = manager.resolve("user", "thread", "repo")

    assert worktree.artifact_root == worktree.root.parent / "artifacts"
    (worktree.artifact_root / "snapshot.yml").write_text("page: ok\n")
    assert git(worktree.root, "status", "--porcelain") == ""


def test_unknown_artifact_workspace_fails_closed(tmp_path: Path) -> None:
    manager = manager_for(tmp_path, git_repo(tmp_path))
    with pytest.raises(ThreadWorktreeError, match="worktree_artifact_not_found"):
        manager.resolve_artifact_root("0" * 32)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp/test_worktree_artifacts.py
```

Expected: FAIL，缺少 `artifact_root` 和组合 backend。

- [ ] **Step 3: 实现目录与受控解析**

```python
class ThreadWorktree(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    root: Path
    artifact_root: Path
    repo_id: str
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    expires_at: datetime


@staticmethod
def _workspace(metadata: _Metadata, root: Path) -> ThreadWorktree:
    artifact_root = root.parent / "artifacts"
    artifact_root.mkdir(mode=0o700, exist_ok=True)
    return ThreadWorktree(
        workspace_ref=metadata.workspace_ref,
        root=root,
        artifact_root=artifact_root,
        repo_id=metadata.repo_id,
        base_commit=metadata.base_commit,
        expires_at=metadata.expires_at,
    )
```

`resolve_artifact_root()` 校验 32 位 ref、metadata 未过期，并确认结果严格等于 `<workspace_root>/<ref>/artifacts`。

- [ ] **Step 4: 用原生 CompositeBackend 暴露虚拟路径**

```python
def create_thread_workspace_backend(manager, repo_id) -> CompositeBackend:
    return CompositeBackend(
        default=ThreadWorktreeBackend(manager, repo_id),
        routes={"/artifacts/": ThreadArtifactBackend(manager, repo_id)},
        artifacts_root="/artifacts/",
    )
```

`ThreadArtifactBackend` 用 `FilesystemBackend(root_dir=current_worktree(self._manager, self._repo_id).artifact_root, virtual_mode=True)` 委托 `ls/read/grep/glob/write/edit/delete`。主 Agent 使用组合 backend；worker 继续只用 `ReadOnlyThreadWorktreeBackend`。

- [ ] **Step 5: 运行 Task 1 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp/test_worktree_artifacts.py \
  tests/tdd/native-worktree-delivery/test_manager_backend.py \
  tests/tdd/unified-assistant-agent/test_read_only_worker.py
```

Expected: PASS；shell cwd 仍是 repo，artifact 不进入 Git。

### Task 2: 用官方 interceptor 实现 Stateful MCP

**Files:**
- Modify: `src/assistant_agent/mcp/config.py`
- Create: `src/assistant_agent/mcp/stateful_sessions.py`
- Modify: `src/assistant_agent/native_agent/tools.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `.local/mcp_servers.json`（仅本机 Playwright 条目）
- Create: `tests/tdd/thread-artifact-stateful-mcp/test_stateful_mcp.py`

**Interfaces:**
- Produces: `MCPServerConfig.session_scope: Literal["call", "thread"] = "call"`。
- Produces: `resolve_mcp_connection(server, worktree=None, discovery_root=None)`。
- Produces: `ThreadMcpSessionPool.call()`、`aclose_workspace()`、`aclose()`。
- Produces: `StatefulMcpInterceptor`；call scope 走 handler，thread scope 走 pool。

- [ ] **Step 1: 写配置与隔离 RED 测试**

```python
def test_call_scope_rejects_thread_tokens() -> None:
    with pytest.raises(ValidationError):
        MCPServerConfig(
            server_name="bad",
            command=["server", "{artifact_root}"],
            session_scope="call",
            allowed_tools=["probe"],
        )


@pytest.mark.asyncio
async def test_pool_reuses_only_the_same_thread(fake_sessions) -> None:
    pool = pool_with(fake_sessions)
    await pool.call(request("user", "thread-a", "browser", "navigate"))
    await pool.call(request("user", "thread-a", "browser", "snapshot"))
    await pool.call(request("user", "thread-b", "browser", "navigate"))
    assert fake_sessions.open_count == 2
```

另测 stateless interceptor 必须调用官方 handler；stateful interceptor 必须跳过 handler并返回 pool 的 `CallToolResult`。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp/test_stateful_mcp.py
```

- [ ] **Step 3: 实现配置和无 shell token 展开**

```python
session_scope: Literal["call", "thread"] = "call"

@model_validator(mode="after")
def validate_session_paths(self) -> MCPServerConfig:
    values = [*self.command, self.cwd or ""]
    if self.session_scope == "call" and any(
        token in value for value in values
        for token in ("{repo_root}", "{artifact_root}")
    ):
        raise ValueError("thread MCP path token requires thread scope")
    return self
```

仅对 argv/cwd 字符串做 `str.replace()`，不构造 shell。thread Tool discovery 用 `TemporaryDirectory` 展开 token，`get_tools()` 返回后清理。

- [ ] **Step 4: 实现 session pool 和 interceptor**

```python
@dataclass
class _SessionEntry:
    workspace_ref: str
    session: ClientSession
    stack: AsyncExitStack
    call_lock: asyncio.Lock


class ThreadMcpSessionPool:
    async def call(self, request: MCPToolCallRequest) -> MCPToolCallResult:
        runtime = request.runtime
        identity = authenticated_user_identity(runtime)
        thread_id = runtime.execution_info.thread_id
        worktree = self._manager.resolve(identity, thread_id, self._repo_id)
        key = (identity, thread_id, self._repo_id, request.server_name)
        entry = await self._entry(key, worktree)
        async with entry.call_lock:
            return await entry.session.call_tool(request.name, request.args)
```

`_entry()` 使用官方 `MultiServerMCPClient({server_name: connection}).session(server_name)` 与 `AsyncExitStack` 持有 session；per-key lock 防止重复创建。异常向上传递，禁止静默回退 stateless。

- [ ] **Step 5: 接入 inventory/owner 并更新本机 Playwright 配置**

composition 顺序变为 manager → pool → inventory；官方 client 的 interceptor 顺序：

```python
tool_interceptors=[stateful_mcp_interceptor, amap_route_link_interceptor]
```

owner 增加 `mcp_session_pool` 并在 `aclose()` 显式关闭。Playwright 本机配置改为：

```json
{
  "session_scope": "thread",
  "cwd": "{repo_root}",
  "command": [
    "/home/lenovo1/.nvm/versions/node/v24.16.0/bin/npx",
    "-y", "@playwright/mcp@0.0.78", "--headless", "--isolated",
    "--output-mode", "stdout",
    "--output-dir", "{artifact_root}/playwright"
  ]
}
```

其他 server、allowlist、read-only 和 env 引用保持原样。

- [ ] **Step 6: 运行 GREEN 与现有 MCP contract**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp/test_stateful_mcp.py \
  tests/core/contract/test_extension_contract.py
```

Expected: PASS；Tool 仍是官方 adapter 转换的 `BaseTool`。

### Task 3: 协调 session 与 worktree TTL

**Files:**
- Modify: `src/assistant_agent/worktree/manager.py`
- Modify: `src/assistant_agent/mcp/stateful_sessions.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `tests/tdd/thread-artifact-stateful-mcp/test_stateful_mcp.py`

**Interfaces:**
- Produces: `expired_workspace_refs()`、`remove_expired(workspace_ref)`。
- Produces: `reap_thread_resources(pool, manager)`，顺序为 close session → remove workspace。

- [ ] **Step 1: 写清理顺序 RED 测试**

```python
@pytest.mark.asyncio
async def test_reaper_closes_session_before_workspace(tmp_path: Path) -> None:
    events: list[str] = []
    await reap_thread_resources(
        fake_pool(on_close=lambda: events.append("session")),
        expired_manager(tmp_path, on_remove=lambda: events.append("workspace")),
    )
    assert events == ["session", "workspace"]
```

- [ ] **Step 2: 拆开同步清理**

从 `resolve()` 删除 `_cleanup_expired()`。`expired_workspace_refs()` 只枚举；`remove_expired()` 在锁内再次复核 TTL，然后执行 `git worktree remove --force` 和 `shutil.rmtree()`。

- [ ] **Step 3: 增加 owner reaper**

```python
async def reap_thread_resources(pool, manager) -> None:
    for ref in manager.expired_workspace_refs():
        await pool.aclose_workspace(ref)
        manager.remove_expired(ref)
```

owner 启动唯一 60 秒周期 task；shutdown 先 cancel/join reaper，再关闭 session，最后清理过期 workspace。测试直接调用单次 helper，不 sleep。

- [ ] **Step 4: 运行 TTL GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp \
  tests/tdd/native-worktree-delivery/test_manager_backend.py
```

### Task 4: 迁移图片生成、HTTP、媒体和 3D 消费

**Files:**
- Modify: `src/assistant_agent/tools/plugins/contracts.py`
- Modify: `src/assistant_agent/native_agent/tools.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/runtime/generated_artifacts.py`
- Modify: `src/assistant_agent/media/generated_artifacts.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/plugin.py`
- Modify: `src/assistant_agent/media/image_to_3d.py`
- Modify: `src/assistant_agent/agent_server/graph.py`
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `src/assistant_agent/agent_server/media_protocol.py`
- Create: `tests/tdd/thread-artifact-stateful-mcp/test_generated_artifacts.py`

**Interfaces:**
- Public ref: `/artifacts/{workspace_ref}/generated/{filename}`。
- Produces: `generated_artifact_location(output_ref, manager)`，统一解析 ref、TTL 与根约束。
- `ToolPluginContext`/`NativeToolResources` 增加 manager/repo_id 受信依赖，不进入 Tool schema。
- `ImageTo3DAdapter` 按 `(user_id, session_id)` 解析当前 thread artifact。

- [ ] **Step 1: 写图片线程隔离 RED 测试**

```python
def test_generated_image_uses_current_thread_artifacts(tmp_path: Path) -> None:
    manager, runtime = runtime_with_worktree(tmp_path, "thread-a")
    message = invoke_fixture_image_tool(manager, runtime)
    ref = message.artifact["images"][0]["output_ref"]
    assert ref.startswith(f"/artifacts/{workspace_ref(runtime)}/generated/")
    assert git(worktree_root(runtime), "status", "--porcelain") == ""


def test_image_id_does_not_cross_threads(tmp_path: Path) -> None:
    manager = manager_for(tmp_path, git_repo(tmp_path))
    first = manager.resolve("user", "thread-a", "repo")
    second = manager.resolve("user", "thread-b", "repo")
    generated = first.artifact_root / "generated"
    generated.mkdir()
    (generated / "image.png").write_bytes(PNG_BYTES)

    assert first.artifact_root != second.artifact_root
    with pytest.raises(ImageTo3DError, match="图片不存在"):
        resolve_image_for_thread(manager, "user", "thread-b", "repo", "image")
```

另覆盖 HTTP 越界/过期 404 和 media payload 读取新 ref。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp/test_generated_artifacts.py
```

- [ ] **Step 3: 图片 Tool 从 runtime 解析根目录**

```python
worktree = manager.resolve(identity, runtime.execution_info.thread_id, repo_id)
generated_root = worktree.artifact_root / "generated"
public_prefix = f"/artifacts/{worktree.workspace_ref}/generated"
result = materialize_image_generation_result(
    result,
    artifact_dir=generated_root,
    public_prefix=public_prefix,
)
```

`_publish_image_ids()` 与 payload 读取必须显式接收本次 root/prefix，不回退全局路径。

- [ ] **Step 4: 删除硬编码 fixture 和全局目录默认值**

删除只被 `DEVELOPMENT_IMAGE_FIXTURE_ID` 使用的 `LocalFixtureImageGenerationAdapter` 与 plugin override。删除 `REPO_ROOT/.local/generated` 默认值；读写函数要求显式 root/prefix，同时保留 MIME、大小和 containment 校验。

- [ ] **Step 5: 迁移消费者**

HTTP endpoint 改为 `/artifacts/{workspace_ref}/generated/{filename}`，从当前 execution owner 的 manager 解析 root。`success_chat_response()` 接受窄 `artifact_payload_resolver` callback；`ImageTo3DAdapter` 用 user/session/repo 定位当前 `generated/`，不跨 thread 搜索 image id。

- [ ] **Step 6: 运行图片 GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp/test_generated_artifacts.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

### Task 5: Authority 与完整验证

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify only if wire changed: `docs/media-agent-service-websocket.md`
- Modify only if routing changed: `docs/authority.toml`

- [ ] **Step 1: 更新当前 authority**

记录 repo/artifact/session 三个边界、CompositeBackend 和清理顺序。只有公开 media wire 或 source glob 实际改变时才修改相邻文档/manifest。

- [ ] **Step 2: 扫描旧路径**

```bash
rg -n 'GENERATED_ARTIFACT_DIR|\.local/playwright-mcp-output|\.local/generated' \
  src tests/core tests/tdd docs/*.md .env.example .local/mcp_servers.json
```

Expected: 无运行时全局目录依赖。

- [ ] **Step 3: 运行定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/thread-artifact-stateful-mcp \
  tests/tdd/native-worktree-delivery \
  tests/tdd/unified-assistant-agent/test_read_only_worker.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py \
  tests/core/contract/test_extension_contract.py \
  tests/core/contract/test_tool_contract.py
```

- [ ] **Step 4: compile 与文档校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/worktree src/assistant_agent/mcp \
  src/assistant_agent/runtime/generated_artifacts.py src/assistant_agent/agent_server
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

- [ ] **Step 5: 验证固定 Playwright 参数与现有 8089**

```bash
env PATH=/home/lenovo1/.nvm/versions/node/v24.16.0/bin:/usr/bin:/bin \
  /home/lenovo1/.nvm/versions/node/v24.16.0/bin/npx \
  -y @playwright/mcp@0.0.78 --help | rg 'output-dir|output-mode'
curl --fail --silent --show-error http://127.0.0.1:8089/ok
```

不得启动第二套 Server；8089 未运行则只报告限制。

- [ ] **Step 6: 检查 diff/提交边界**

```bash
git status --short
git diff --check
```

artifact 文件不得出现在 status。因另一项未提交重构与本任务共享 `services.py` 和 authority，默认不自动提交；只有能精确拆分且不夹带其他改动时才提交本任务源码/测试。设计和计划文档保持未提交。
