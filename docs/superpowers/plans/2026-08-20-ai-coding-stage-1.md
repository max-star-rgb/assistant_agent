# AI Coding 阶段 1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 thread-scoped 临时 Git worktree 中实现代码检查、候选 patch、确定性校验、人工审批和原子应用的最小编辑闭环。

**Architecture:** 保留唯一 `AssistantRootGraph`，增加结构化 `coding` 路由和顺序 `AssistantCodingGraph`。模型只能调用受根目录约束的只读 coding Tool 和无副作用的 `coding_propose_patch`；workspace 解析、patch 校验、HITL 和实际应用由受信服务及确定性 Graph 节点负责。

**Tech Stack:** Python 3.12、Pydantic、LangChain `create_agent` / `BaseTool`、LangGraph `StateGraph` / `interrupt` / `Command`、Git CLI 固定 argv、pytest。

**Spec:** `docs/superpowers/specs/2026-08-20-ai-coding-agent-design.md`

## Global Constraints

- 生产主入口仍为 `AssistantRootGraph`，不得引入第二套 Runtime 或自建 checkpoint/resume 协议。
- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、offline；本阶段不调用真实 Provider或网络。
- coding 模式只由结构化 `execution_mode="coding"` 选择，不从自然语言推断。
- 每个 `user_identity + thread_id + source_repo_id` 解析到独立临时 Git worktree。
- 模型不可见宿主绝对路径、身份字段、thread ID、shell、直接写文件、删除、commit、merge 或 push。
- 阶段 1 只允许修改既有 UTF-8 文本文件和创建白名单后缀的新 UTF-8 文本文件。
- patch 审批绑定 `base_commit + base_file_digests + patch_digest`；批准后不能替换内容。
- apply 必须在 workspace 独占锁中重新校验并保持失败原子性。
- feature RED/GREEN 仅写入独立 `tests/tdd/ai-coding-*`；仅 `LOOP-001`、`CTX-001` 更新现有 core 测试。
- 不增加 Deep Agents 运行时依赖；只借鉴其 backend、权限和 thread-scoped workspace 设计。

---

## 文件职责图

- `src/assistant_agent/coding/models.py`：workspace、proposal、validation、approval、apply result 的冻结 Pydantic 契约。
- `src/assistant_agent/coding/config.py`：coding enablement、source repo allowlist、workspace root、TTL 和资源上限。
- `src/assistant_agent/coding/policy.py`：相对路径、后缀、protected glob 和 symlink 边界。
- `src/assistant_agent/coding/patches.py`：严格 unified diff 子集解析、摘要和 digest。
- `src/assistant_agent/coding/workspace.py`：worktree 生命周期、安全读取/搜索、Git 状态、patch dry-run/apply/恢复。
- `src/assistant_agent/coding/tools.py`：只读 Tool 与 `coding_propose_patch` 的标准 `BaseTool` 工厂。
- `src/assistant_agent/native_agent/coding_graph.py`：顺序 coding graph、确定性 validation、interrupt/resume 和 terminal summary。
- `src/assistant_agent/native_agent/state.py`：公开 coding 输入、父图 channel 和 `CodingState`。
- `src/assistant_agent/native_agent/root_graph.py`：结构化 `coding` 路由。
- `src/assistant_agent/agent_server/services.py`：进程级 workspace service、coding tools/agent/graph 的静态装配与关闭。

---

### Task 1: Coding 契约、配置与路径策略

**Files:**
- Create: `src/assistant_agent/coding/__init__.py`
- Create: `src/assistant_agent/coding/models.py`
- Create: `src/assistant_agent/coding/config.py`
- Create: `src/assistant_agent/coding/policy.py`
- Create: `tests/tdd/ai-coding-workspace/test_config_and_policy.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `CodingConfig.from_env(env: Mapping[str, str] | None = None) -> CodingConfig`
- Produces: `CodingPathPolicy.validate_relative_path(root: Path, raw_path: str, *, operation: Literal["read", "write"]) -> Path`
- Produces: `CodingPatchProposal`、`CodingPatchValidation`、`CodingApprovalDecision`、`CodingPatchApplyResult`、`CodingTerminalResult`

- [ ] **Step 1: 写配置与路径策略的 RED 测试**

```python
def test_coding_config_requires_allowlisted_absolute_repo(tmp_path: Path) -> None:
    env = {
        "MULTIMODAL_AGENT_CODING_ENABLED": "true",
        "MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON": json.dumps(
            {"assistant-agent": {"path": str(tmp_path / "repo"), "target_branch": "main"}}
        ),
    }
    config = CodingConfig.from_env(env)
    assert config.repositories["assistant-agent"].target_branch == "main"


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", ".git/config", ".env"])
def test_write_policy_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(CodingPolicyError):
        CodingPathPolicy().validate_relative_path(tmp_path, path, operation="write")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-workspace/test_config_and_policy.py
```

Expected: FAIL，缺少 `assistant_agent.coding` 模块。

- [ ] **Step 3: 实现冻结契约和显式配置**

核心配置必须采用 permissive-disabled 默认值，并在显式启用但缺少 repo allowlist 时立即失败：

```python
class CodingRepositoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    repo_id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    path: Path
    target_branch: str = Field(min_length=1, max_length=160)


class CodingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    enabled: bool = False
    workspace_root: Path
    repositories: dict[str, CodingRepositoryConfig] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=86_400, ge=300, le=604_800)
    max_patch_bytes: int = Field(default=262_144, ge=1_024, le=1_048_576)
    max_changed_files: int = Field(default=32, ge=1, le=256)
    max_file_bytes: int = Field(default=2_097_152, ge=1_024, le=10_485_760)
```

`from_env` 只读取以下变量：

```text
MULTIMODAL_AGENT_CODING_ENABLED=false
MULTIMODAL_AGENT_CODING_WORKSPACE_ROOT=/absolute/untracked/path
MULTIMODAL_AGENT_CODING_REPOSITORIES_JSON={}
MULTIMODAL_AGENT_CODING_TTL_SECONDS=86400
MULTIMODAL_AGENT_CODING_MAX_PATCH_BYTES=262144
MULTIMODAL_AGENT_CODING_MAX_CHANGED_FILES=32
MULTIMODAL_AGENT_CODING_MAX_FILE_BYTES=2097152
```

`path` 和 `workspace_root` 必须是绝对路径；启用时 source repo 必须存在、为 Git worktree，workspace root 不得位于任何 source repo 内。

- [ ] **Step 4: 实现 fail-closed 路径策略**

```python
class CodingPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


DEFAULT_WRITABLE_SUFFIXES = frozenset({
    ".cfg", ".css", ".go", ".html", ".ini", ".java", ".js", ".json",
    ".jsx", ".md", ".py", ".rs", ".sh", ".sql", ".toml", ".ts", ".tsx",
    ".txt", ".xml", ".yaml", ".yml",
})
DEFAULT_PROTECTED_GLOBS = (".git/**", ".env", ".env.*", "**/*.pem", "**/*.key")
```

先做 lexical validation，再执行 `candidate.resolve(strict=False)` 和 `relative_to(root.resolve())`。写操作遇到已存在 symlink 或父目录 symlink 时返回 `symlink_escape`；不把异常中的宿主路径放入公开 message。

- [ ] **Step 5: 运行 Task 1 测试并确认 GREEN**

Run: 使用 Step 2 的同一命令。

Expected: PASS。

- [ ] **Step 6: 提交 Task 1**

```bash
git add .env.example src/assistant_agent/coding tests/tdd/ai-coding-workspace/test_config_and_policy.py
git commit -m "feat: define coding workspace policy"
```

---

### Task 2: Thread-scoped Git worktree 与安全只读能力

**Files:**
- Modify: `src/assistant_agent/coding/models.py`
- Create: `src/assistant_agent/coding/workspace.py`
- Create: `tests/tdd/ai-coding-workspace/test_workspace_lifecycle.py`
- Create: `tests/tdd/ai-coding-workspace/test_workspace_reads.py`

**Interfaces:**
- Consumes: `CodingConfig`、`CodingPathPolicy`
- Produces: `CodingWorkspaceService.resolve(identity: str, thread_id: str, repo_id: str) -> CodingWorkspace`
- Produces: `list_files(...)`、`search(...)`、`read(...)`、`status(...)`、`diff(...)`
- Produces: `CodingWorkspaceService.aclose() -> None`

- [ ] **Step 1: 写 workspace 生命周期 RED 测试**

```python
def test_same_scope_reuses_worktree_and_other_scope_isolated(git_repo, coding_config) -> None:
    service = CodingWorkspaceService(coding_config)
    first = service.resolve("user-a", "thread-a", "repo")
    again = service.resolve("user-a", "thread-a", "repo")
    other = service.resolve("user-b", "thread-a", "repo")
    assert first.workspace_ref == again.workspace_ref
    assert first.root == again.root
    assert other.workspace_ref != first.workspace_ref
    assert first.base_commit == git_repo.head_sha
```

测试 fixture 用固定 argv 初始化临时 Git 仓库并提交两个 UTF-8 文件；不要依赖开发仓库当前状态。

- [ ] **Step 2: 写只读边界 RED 测试**

覆盖：分页 list、literal search、按行 read、status/diff 有界输出、身份错配、过期 workspace、symlink、隐藏路径、文件过大和无效 UTF-8。

```python
def test_workspace_identity_mismatch_fails(git_repo, coding_config) -> None:
    service = CodingWorkspaceService(coding_config)
    workspace = service.resolve("user-a", "thread-a", "repo")
    with pytest.raises(CodingWorkspaceError, match="workspace_identity_mismatch"):
        service.get(workspace.workspace_ref, identity="user-b", thread_id="thread-a")
```

- [ ] **Step 3: 运行 workspace 测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-workspace
```

Expected: FAIL，缺少 `CodingWorkspaceService`。

- [ ] **Step 4: 实现确定性 workspace identity 与 metadata**

`workspace_ref` 使用 HMAC-SHA256 派生的 opaque ID；HMAC key 在 service 进程内生成，不写入模型上下文。目录中保存 `metadata.json`，字段固定为：

```python
class CodingWorkspaceMetadata(BaseModel):
    schema_version: Literal["coding_workspace_v1"] = "coding_workspace_v1"
    workspace_ref: str
    identity_digest: str
    thread_digest: str
    repo_id: str
    base_commit: str
    created_at: datetime
    expires_at: datetime
    frozen: bool = False
```

创建命令只能由 backend 组装：

```python
["git", "-C", str(source_repo), "worktree", "add", "--detach", str(root), base_commit]
```

所有 subprocess 调用使用 `shell=False`、干净 env、固定 timeout、有界 stdout/stderr。公开错误只返回稳定错误码。每个 workspace 使用 `fcntl.flock` 的 `.coding.lock`；锁文件位于 workspace 管理目录而不是受模型访问的 repo 树。

- [ ] **Step 5: 实现安全只读方法**

`list_files` 使用 `os.scandir`，不跟随 symlink，按 path 排序并用整数 cursor 分页。`search` 使用 Python 逐文件 literal substring 搜索，限制 glob、文件数量、总读取字节和命中数量；阶段 1 不接受正则。`read` 返回行号、起止范围、total lines 和 next cursor。`status/diff` 只执行固定 Git argv：

```python
["git", "-C", root, "status", "--porcelain=v1", "--untracked-files=all"]
["git", "-C", root, "diff", "--no-ext-diff", "--no-color", "--"]
```

- [ ] **Step 6: 实现 TTL 清理与关闭**

`resolve` 前调用 `cleanup_expired()`；只清理由 metadata 证明属于本 service 配置、已过期且未加锁的 worktree。删除通过 source repo 对应的固定 `git worktree remove --force <exact-root>` 完成。`aclose()` 只释放 service 自身资源，不删除仍在 TTL 内的 workspace。

- [ ] **Step 7: 运行 Task 2 测试并确认 GREEN**

Run: 使用 Step 3 的同一命令。

Expected: PASS。

- [ ] **Step 8: 提交 Task 2**

```bash
git add src/assistant_agent/coding tests/tdd/ai-coding-workspace
git commit -m "feat: add isolated coding worktrees"
```

---

### Task 3: 严格 Patch 解析、校验与失败原子应用

**Files:**
- Modify: `src/assistant_agent/coding/models.py`
- Create: `src/assistant_agent/coding/patches.py`
- Modify: `src/assistant_agent/coding/workspace.py`
- Create: `tests/tdd/ai-coding-patch/test_patch_validation.py`
- Create: `tests/tdd/ai-coding-patch/test_patch_apply.py`

**Interfaces:**
- Produces: `parse_coding_patch(patch: str, *, policy: CodingPathPolicy, root: Path, limits: CodingConfig) -> ParsedCodingPatch`
- Produces: `validate_patch(workspace: CodingWorkspace, patch: str, summary: str) -> CodingPatchValidation`
- Produces: `apply_validated_patch(workspace: CodingWorkspace, validation: CodingPatchValidation) -> CodingPatchApplyResult`

- [ ] **Step 1: 写严格 patch subset 的 RED 测试**

接受普通修改和 `new file mode 100644` 新文本文件。拒绝 deletion、rename、copy、mode change、binary patch、quoted/escaped path、绝对路径、protected path、重复目标、超限文件数和超限字节。

```python
def test_validation_derives_paths_and_digest(workspace_service, workspace) -> None:
    result = workspace_service.validate_patch(
        workspace,
        VALID_UPDATE_PATCH,
        summary="change greeting",
    )
    assert result.status == "valid"
    assert result.changed_paths == ("src/app.py",)
    assert result.patch_digest == hashlib.sha256(
        VALID_UPDATE_PATCH.encode("utf-8")
    ).hexdigest()
```

- [ ] **Step 2: 写审批漂移和失败原子性的 RED 测试**

```python
def test_apply_rejects_file_digest_drift_without_partial_write(service, workspace) -> None:
    validation = service.validate_patch(workspace, TWO_FILE_PATCH, "summary")
    (workspace.root / "a.py").write_text("external-change\n", encoding="utf-8")
    before_b = (workspace.root / "b.py").read_bytes()
    with pytest.raises(CodingWorkspaceError, match="file_digest_changed"):
        service.apply_validated_patch(workspace, validation)
    assert (workspace.root / "b.py").read_bytes() == before_b
```

同时覆盖 base commit 漂移、digest mismatch、`git apply --check` 冲突和模拟 apply 中途异常后的恢复。

- [ ] **Step 3: 运行 patch 测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-patch
```

Expected: FAIL，缺少 parser/validator/apply。

- [ ] **Step 4: 实现严格 unified diff parser**

parser 只接受 UTF-8、LF、`diff --git a/<path> b/<path>`、匹配的 `---/+++` 和标准 hunk。路径不得带引号、反斜杠转义或 NUL。禁止头字段：

```python
FORBIDDEN_PATCH_HEADERS = (
    "deleted file mode ", "old mode ", "new mode ", "rename from ",
    "rename to ", "copy from ", "copy to ", "similarity index ",
    "dissimilarity index ", "GIT binary patch", "Binary files ",
)
```

新文件只允许 `new file mode 100644`、`--- /dev/null`、`+++ b/<path>`。所有授权字段从 parser 结果派生，不信任模型声明。

- [ ] **Step 5: 实现 validation**

在 workspace 独占锁中读取当前 `HEAD`、目标文件 bytes 和 SHA-256，调用：

```python
["git", "-C", root, "apply", "--check", "--whitespace=nowarn", "--"]
```

patch 通过 stdin 传入，不拼接 shell 字符串。输出 `CodingPatchValidation(status="valid", patch=..., patch_digest=..., base_commit=..., base_file_digests=..., changed_paths=...)`；公开 artifact 保留完整结构化值，模型 observation 只返回摘要和有界 diff preview。

- [ ] **Step 6: 实现 apply 与恢复**

在同一 workspace 独占锁内重新校验 `HEAD`、目标文件摘要和 patch digest，保存既有目标文件 byte snapshot，并记录原本不存在的新文件。再次 `git apply --check` 后运行固定 `git apply --whitespace=nowarn --`。异常时逐一恢复 snapshot、删除本轮新文件，再验证 `git diff` 与 apply 前一致；恢复验证失败时写入 `frozen=true` 并抛出 `rollback_failed`。

- [ ] **Step 7: 运行 Task 3 测试并确认 GREEN**

Run: 使用 Step 3 的同一命令。

Expected: PASS。

- [ ] **Step 8: 提交 Task 3**

```bash
git add src/assistant_agent/coding tests/tdd/ai-coding-patch
git commit -m "feat: validate and apply coding patches"
```

---

### Task 4: Coding 专属标准 Tool Inventory

**Files:**
- Create: `src/assistant_agent/coding/tools.py`
- Create: `tests/tdd/ai-coding-graph/test_coding_tools.py`

**Interfaces:**
- Consumes: `CodingWorkspaceService`
- Produces: `create_coding_tools(service: CodingWorkspaceService) -> list[BaseTool]`
- Produces Tool names: `coding_repo_list`、`coding_repo_search`、`coding_repo_read`、`coding_repo_status`、`coding_repo_diff`、`coding_propose_patch`

- [ ] **Step 1: 写 schema、身份注入和结果边界 RED 测试**

```python
def test_coding_tool_schema_hides_runtime_scope(coding_tools) -> None:
    by_name = {tool.name: tool for tool in coding_tools}
    schema = by_name["coding_repo_read"].tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"path", "start_line", "end_line"}
    assert by_name["coding_repo_read"].metadata == {
        "effect": "read", "source": "builtin"
    }
    assert by_name["coding_propose_patch"].metadata == {
        "effect": "generate", "source": "builtin"
    }
```

增加 fake `ToolRuntime` 测试，证明身份来自 `server_info.user.identity`，thread 来自 runtime config，repo ID 来自受信 state，不接受模型参数覆盖。

- [ ] **Step 2: 运行 coding Tool 测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-graph/test_coding_tools.py
```

Expected: FAIL，缺少 `create_coding_tools`。

- [ ] **Step 3: 实现 ToolRuntime scope helper**

```python
def coding_scope(runtime: ToolRuntime[AssistantRunContext]) -> CodingToolScope:
    identity = authenticated_user_identity(runtime)
    thread_id = str(runtime.config.get("configurable", {}).get("thread_id", "")).strip()
    repo_id = str(runtime.state.get("coding_repo_id", "")).strip()
    if not thread_id or not repo_id:
        raise ToolException("coding_scope_unavailable")
    return CodingToolScope(identity=identity, thread_id=thread_id, repo_id=repo_id)
```

- [ ] **Step 4: 用 `@tool` factory 实现六个 Tool**

所有 Tool 使用 `response_format="content_and_artifact"`、`invoke_native_tool` 和 `configure_builtin_tool`。读取 Tool 返回分页结构化 artifact；`coding_propose_patch(patch, summary, runtime)` 只调用 validation 并返回 proposal/validation artifact，不调用 apply。

- [ ] **Step 5: 运行 Task 4 测试并确认 GREEN**

Run: 使用 Step 2 的同一命令。

Expected: PASS。

- [ ] **Step 6: 提交 Task 4**

```bash
git add src/assistant_agent/coding/tools.py tests/tdd/ai-coding-graph/test_coding_tools.py
git commit -m "feat: expose governed coding tools"
```

---

### Task 5: 顺序 AssistantCodingGraph 与 Digest-bound HITL

**Files:**
- Modify: `src/assistant_agent/native_agent/models.py`
- Modify: `src/assistant_agent/native_agent/state.py`
- Create: `src/assistant_agent/native_agent/coding_graph.py`
- Create: `tests/tdd/ai-coding-graph/test_coding_graph.py`

**Interfaces:**
- Consumes: `BaseChatModel`、coding `BaseTool` inventory、`CodingWorkspaceService`
- Produces: `build_coding_graph(model, tools, workspace_service, ...) -> CompiledStateGraph`
- Produces: `CodingState` 与父图可见 `coding_result: CodingTerminalResult`

- [ ] **Step 1: 写 graph RED 测试**

使用 scripted mock model：首次调用 `coding_repo_read`，其次调用 `coding_propose_patch`，最后不再生成 Tool call。断言 graph 在 apply 前 interrupt，worktree 未变化，payload 的 action/digest/paths 正确。

```python
assert interrupted["__interrupt__"][0].value["action"] == "coding_patch_apply"
assert interrupted["__interrupt__"][0].value["patch_digest"] == expected_digest
assert workspace_file.read_text() == "before\n"
```

分别覆盖：approve 后只应用该 digest；reject 不写入；respond 清除旧 proposal 并带用户意见返回 draft；恢复前文件漂移导致 `file_digest_changed`；无 proposal 的模型回复以结构化失败结束。

- [ ] **Step 2: 运行 graph 测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-graph/test_coding_graph.py
```

Expected: FAIL，缺少 `AssistantCodingGraph`。

- [ ] **Step 3: 扩展输入与 state**

```python
ExecutionMode = Literal["fast", "planning", "coding"]

class AssistantRootInput(BaseModel):
    messages: list[AnyMessage]
    execution_mode: ExecutionMode = "fast"
    coding_repo_id: str | None = None

    @model_validator(mode="after")
    def require_coding_repo(self):
        if self.execution_mode == "coding" and not self.coding_repo_id:
            raise ValueError("coding_repo_id is required in coding mode")
        return self
```

`AssistantRootState` 增加 `coding_repo_id` 和 `coding_result`；`CodingState` 只包含 messages、memory/trusted facts、repo/workspace/proposal/validation/approval/applied result，不复用 planning worker channels。

- [ ] **Step 4: 构建 coding inspect agent**

使用 `create_agent(model=model, tools=tools, state_schema=CodingState, context_schema=AssistantRunContext, middleware=[dynamic coding prompt, ModelCallLimitMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware(read tools), SummarizationMiddleware])`。coding prompt 明确要求先检查、再提交单份完整 patch；安全性仍由 backend 和 graph 保证。

- [ ] **Step 5: 实现确定性 graph 节点与边**

```text
START -> resolve_workspace -> inspect_and_draft -> validate_proposal
validate_proposal -> approval | summarize
approval --Command--> inspect_and_draft | apply_patch | summarize
apply_patch -> summarize -> END
```

`approval` 调用 `interrupt(payload)`，随后用 `CodingApprovalDecision.model_validate()` 解析。`approve` 要求响应 digest 等于 payload digest；`respond` 将有界用户意见作为新 `HumanMessage` 添加，清空 proposal/validation；`reject` 写入 terminal result。`apply_patch` 不调用模型。`summarize` 始终追加标准 `AIMessage`，并写入结构化 `coding_result`。

- [ ] **Step 6: 运行 Task 5 测试并确认 GREEN**

Run: 使用 Step 2 的同一命令。

Expected: PASS。

- [ ] **Step 7: 提交 Task 5**

```bash
git add src/assistant_agent/native_agent/models.py src/assistant_agent/native_agent/state.py src/assistant_agent/native_agent/coding_graph.py tests/tdd/ai-coding-graph/test_coding_graph.py
git commit -m "feat: add coding graph approval loop"
```

---

### Task 6: 接入唯一生产父图与 Agent Server Composition

**Files:**
- Modify: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Create: `tests/tdd/ai-coding-graph/test_production_composition.py`

**Interfaces:**
- Consumes: `build_coding_graph`、`CodingConfig.from_env`、`CodingWorkspaceService`、`create_coding_tools`
- Changes: `build_assistant_root_graph(..., coding_graph: Any, ...)`
- Changes: `AgentServerExecutionOwner` owns and closes `coding_workspace_service`

- [ ] **Step 1: 写 production composition RED 测试**

断言静态父图包含唯一 `coding_graph` 节点、`route_execution_mode` 精确返回三种模式、coding inventory 不包含普通 assistant Tool、disabled config 下 coding run 返回结构化 unconfigured 结果且不创建 worktree。

- [ ] **Step 2: 运行 composition 测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-graph/test_production_composition.py
```

Expected: FAIL，父图没有 coding branch。

- [ ] **Step 3: 装配 coding 专属资源**

在 `AgentServerExecutionOwner.compose` 中独立构造：

```python
coding_config = CodingConfig.from_env()
coding_workspace_service = CodingWorkspaceService(coding_config)
coding_tools = create_coding_tools(coding_workspace_service)
coding_graph = build_coding_graph(
    model,
    coding_tools,
    coding_workspace_service,
    model_call_limit=config.max_tool_iterations,
    tool_call_limit=config.max_tool_iterations,
)
```

普通 `tools` inventory 保持不变；coding tools 不加入 fast/planning inventory。`aclose` 显式关闭 workspace service。

- [ ] **Step 4: 扩展父图路由**

路由表改为：

```python
{"fast": "fast_agent", "planning": "planning_graph", "coding": "coding_graph"}
```

三个执行分支都只连向同一个 `refresh_memory_extraction`。`route_execution_mode` 对 schema 内三种值精确返回，不再用“非 planning 即 fast”的吞错逻辑。

- [ ] **Step 5: 运行 Task 6 测试并确认 GREEN**

Run: 使用 Step 2 的同一命令。

Expected: PASS。

- [ ] **Step 6: 提交 Task 6**

```bash
git add src/assistant_agent/native_agent/root_graph.py src/assistant_agent/agent_server/services.py tests/tdd/ai-coding-graph/test_production_composition.py
git commit -m "feat: route coding runs through native graph"
```

---

### Task 7: 更新核心不变量保护

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_context_lifecycle.py`

**Interfaces:**
- Changes: `LOOP-001` 登记 fast/planning/coding 三个结构化分支
- Changes: `CTX-001` 登记 coding patch 的 digest-bound 原生 HITL

- [ ] **Step 1: 先更新现有 core 测试的稳定结构断言**

将父图节点断言加入 `coding_graph`，并断言其 name 为 `AssistantCodingGraph`。保留 fast/planning 既有行为测试；新增 coding core probe 只能使用临时 repo、scripted model 和无语义 sentinel，不导入具体业务 prompt。

- [ ] **Step 2: 增加 CTX-001 的 digest-bound resume probe**

测试只断言结构化事实：interrupt action、批准前无副作用、错误 digest 不应用、正确 digest 恢复后一次应用、terminal 为标准 `AIMessage`。不要断言完整自然语言文本或私有方法调用次数。

- [ ] **Step 3: 更新 invariant 文本**

`LOOP-001` 明确 coding 是显式顺序 StateGraph；`CTX-001` 明确 fast 自动放行、planning 非 read Tool HITL、coding patch 在确定性节点执行 digest-bound HITL。

- [ ] **Step 4: 运行受影响 core 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_context_lifecycle.py
```

Expected: PASS。

- [ ] **Step 5: 提交 Task 7**

```bash
git add tests/core/INVARIANTS.md tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_context_lifecycle.py
git commit -m "test: protect native coding graph invariants"
```

---

### Task 8: 同步 Authority、执行最小总验收并检查 reload

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Documents: coding graph、workspace backend、coding Tool inventory、HITL、配置和阶段 1 非目标
- Changes manifest: coding source globs 分别归入 runtime、tool-calling、agent-server 的既有 authority

- [ ] **Step 1: 更新三个 owner authority**

`runtime-event-stream` 记录三分支父图和顺序 CodingGraph；`tool-calling` 记录 coding tools 与“apply 不是模型 Tool”；`agent-server` 记录 process-owned workspace service、thread/identity scope 和 TTL。不要把 spec 复制为第二份事实权威。

- [ ] **Step 2: 更新 manifest source globs 与 verification**

把 `src/assistant_agent/native_agent/coding_graph.py` 归入 runtime，把 `src/assistant_agent/coding/tools.py`、`policy.py`、`patches.py` 归入 tool-calling，把 `src/assistant_agent/coding/config.py`、`workspace.py` 归入 agent-server。为三个 domain 增加对应定向 TDD 命令，不让 `docs/superpowers/**` 成为 authority。

- [ ] **Step 3: 运行全部阶段 1 定向测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/ai-coding-workspace tests/tdd/ai-coding-patch tests/tdd/ai-coding-graph tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_context_lifecycle.py
```

Expected: PASS；无网络和真实 Provider 调用。

- [ ] **Step 4: 运行 authority validator**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

Expected: PASS。

- [ ] **Step 5: 检查唯一 8089 dev server 的 hot reload**

作为客户端请求现有 `8089` 服务的健康/assistants 只读入口，确认服务已 reload 且仍只运行一套 dev server。若服务未运行，先报告而不是另起并行实例；需要完整重启时只能使用 `scripts/run_server.py` 的单实例入口。

- [ ] **Step 6: 检查阶段 1 安全负面能力**

通过结构化 Tool inventory 和一次 mock coding run 确认不存在 coding shell、delete、commit、merge、push；确认 fast/planning inventory 没有意外获得 coding Tool；确认 disabled coding config 不创建 workspace。

- [ ] **Step 7: 提交 Task 8**

```bash
git add docs/runtime-event-stream-architecture.md docs/tool-calling-architecture.md docs/agent-server-architecture.md docs/authority.toml
git commit -m "docs: document coding stage one boundaries"
```

## 完成汇报格式

```text
完成：AI Coding 阶段 1 最小编辑闭环。
Core invariant: LOOP-001 and CTX-001 changed to cover the native coding branch and digest-bound patch approval.
Tests: added tests/tdd/ai-coding-workspace, tests/tdd/ai-coding-patch, and tests/tdd/ai-coding-graph for temporary RED/GREEN; user may delete these directories manually.
Validation: <列出实际执行的命令与结果>。
Provider: 未调用真实 Provider；全部验证使用 mock/offline。
Limitations: 不执行命令、不删除文件、不 commit、不 merge、不 push；变更只保留在 thread-scoped 临时 worktree。
```
