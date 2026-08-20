# AI Coding 阶段 4A 本地容器强隔离实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 coding test、lint、format、build 从宿主受限 subprocess 迁入默认断网、非 root、资源受限的本地 Docker 容器，并保持既有 formatter HITL 与 commit/merge 顺序不变。

**Architecture:** 新增可替换的 `CodingSandboxBackend` 与 Docker CLI 实现，由 Agent Server process owner 静态持有并注入 `CodingValidationService`。repository 显式启用 sandbox 后，validation 只通过 digest-pinned 本地镜像执行固定 argv；任何 daemon、image、resource 或 cleanup 错误均 fail closed，绝不回退宿主执行。

**Tech Stack:** Python 3.12、Pydantic、`Protocol`、Docker Engine/CLI、固定 subprocess argv、LangGraph 既有 CodingGraph、pytest。

**Spec:** `docs/superpowers/specs/2026-08-20-ai-coding-stage-4a-sandbox-design.md`

## Global Constraints

- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；pytest 与验收不得访问真实 Provider、网络或付费服务。
- sandbox 默认关闭；只允许 repository 静态配置启用。
- image 必须是本机预置的 `<repository>@sha256:<64 lowercase hex>`；禁止 tag-only、自动 pull/build 和 latest。
- 模型、客户端、messages、Runtime Context、Tool schema 与 resume payload 不能提交 image、argv、environment、mount、user、network 或 Docker flags。
- sandbox 启用后禁止静默回退宿主 subprocess；任何 sandbox 失败都阻止 controlled commit/merge。
- 容器必须 `network none`、非 root、只读 rootfs、drop all capabilities、no-new-privileges、默认 seccomp，且不能挂载 Docker socket、source repository、coding worktree 或 Agent Server cwd。
- 阶段 4A 不提供网络、依赖安装、secret 注入、push、PR、fetch/pull、部署或冲突修复。
- 新测试仅写入 `tests/tdd/ai-coding-sandbox/`，不加入默认 pytest、不提交该临时目录。
- `Core invariant: unchanged`；除非实现实际改变 Graph 路由、interrupt schema 或 integration 顺序，否则不得修改 `tests/core`。
- 不启动、停止或重启 8089；只在最终验收时等待 hot reload 并作为客户端检查 `/ok`。

---

## 文件职责图

- `src/assistant_agent/coding/config.py`：repository sandbox enablement、digest image 和 CPU quota 配置校验。
- `src/assistant_agent/coding/models.py`：冻结的 sandbox request/result 与扩展 command evidence 契约。
- `src/assistant_agent/coding/sandbox.py`：backend Protocol、Docker CLI 生命周期、安全 argv、资源监测和清理。
- `src/assistant_agent/coding/validation.py`：在 sandbox/host 两种显式模式间选择，并把 sandbox result 投影为既有 verification evidence。
- `src/assistant_agent/agent_server/services.py`：唯一 backend 的 process-owned composition 与关闭顺序。
- `.env.example`：disabled-by-default、digest-pinned repository 配置示例。
- `tests/tdd/ai-coding-sandbox/`：阶段 4A 临时 RED/GREEN，不提交。
- `docs/agent-server-architecture.md`：sandbox owner、容器生命周期、默认断网和 no-fallback 权威。
- `docs/tool-calling-architecture.md`：sandbox execution 不是模型 Tool 的边界。
- `docs/authority.toml`：新增源码与 TDD 的 owner/verification 路由。

---

### Task 1: 定义 sandbox 配置与稳定契约

**Files:**
- Modify: `src/assistant_agent/coding/config.py`
- Modify: `src/assistant_agent/coding/models.py`
- Modify: `.env.example`
- Create: `tests/tdd/ai-coding-sandbox/test_sandbox_contracts.py`

**Interfaces:**
- Changes: `CodingCommandConfig.cpu_cores: float = 1.0`
- Changes: `CodingRepositoryConfig.sandbox_enabled: bool = False`
- Changes: `CodingRepositoryConfig.sandbox_image: str | None = None`
- Produces: `CodingSandboxRequest`
- Produces: `CodingSandboxResult`
- Changes: `CodingCommandEvidence` accepts sandbox resource facts without container/host identifiers

- [ ] **Step 1: 写配置与 schema RED 测试**

```python
def test_sandbox_requires_digest_pinned_image_and_verification() -> None:
    with pytest.raises(ValidationError):
        CodingRepositoryConfig(
            repo_id="repo",
            path=repo_path,
            target_branch="main",
            sandbox_enabled=True,
            sandbox_image="python:3.12",
        )


def test_sandbox_contract_has_no_runtime_override_fields() -> None:
    fields = CodingSandboxRequest.model_fields
    assert set(fields) == {
        "image", "argv", "scratch_root", "command_id", "kind",
        "timeout_seconds", "cpu_seconds", "cpu_cores", "memory_bytes",
        "max_processes", "max_output_bytes", "max_file_bytes", "max_disk_bytes",
    }
    assert not ({"network", "mounts", "environment", "user", "docker_flags"} & fields.keys())
```

同时覆盖：sandbox 默认关闭；启用时 image 必填、sequence 非空；拒绝 tag、uppercase/短 digest、控制字符；
`cpu_cores` 拒绝 `<0.1` 和 `>16.0`；公开 root input/context/resume schema 不出现 sandbox 字段。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_sandbox_contracts.py
```

Expected: FAIL，因为 sandbox 字段与 request/result 尚不存在。

- [ ] **Step 3: 实现冻结配置与 Pydantic 契约**

```python
_SANDBOX_IMAGE_PATTERN = r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"

class CodingSandboxRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    image: str = Field(pattern=_SANDBOX_IMAGE_PATTERN)
    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    scratch_root: Path
    command_id: str
    kind: Literal["test", "lint", "format", "build"]
    timeout_seconds: int
    cpu_seconds: int
    cpu_cores: float
    memory_bytes: int
    max_processes: int
    max_output_bytes: int
    max_file_bytes: int
    max_disk_bytes: int

class CodingSandboxResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    status: Literal["passed", "failed", "timed_out", "resource_exceeded"]
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    output_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    oom_killed: bool = False
    error_code: str | None = None
    cleanup_status: Literal["not_created", "removed", "failed"]
```

repository validator 要求 `sandbox_enabled` 时 image 与 sequence 同时存在。`CodingConfig` 在 sandbox repository
存在时拒绝包含 NUL、换行、回车或逗号的 `workspace_root`，避免 Docker CLI `--mount` 参数歧义。

- [ ] **Step 4: 更新 disabled-by-default 示例并运行 GREEN**

`.env.example` 的 repository JSON 只展示：

```json
{"sandbox_enabled":false,"sandbox_image":null}
```

注释说明启用值必须是 operator 已预置的 digest，不填写真实环境 image。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_sandbox_contracts.py
```

Expected: PASS。

- [ ] **Step 5: 提交生产契约，不提交临时 TDD**

```bash
git add .env.example src/assistant_agent/coding/config.py src/assistant_agent/coding/models.py
git commit -m "feat: define coding sandbox contracts"
```

---

### Task 2: 实现 fail-closed Docker sandbox backend

**Files:**
- Create: `src/assistant_agent/coding/sandbox.py`
- Create: `tests/tdd/ai-coding-sandbox/conftest.py`
- Create: `tests/tdd/ai-coding-sandbox/test_docker_sandbox.py`

**Interfaces:**
- Consumes: `CodingSandboxRequest`、`CodingSandboxResult`
- Produces: `CodingSandboxBackend.execute(request) -> CodingSandboxResult`
- Produces: `DockerCodingSandboxBackend(docker_binary="docker", *, owner_id=None, poll_interval_seconds=0.1)`
- Produces: `DockerCodingSandboxBackend.aclose() -> None`

- [ ] **Step 1: 写安全 argv、image preflight 与 no-pull RED 测试**

```python
def test_create_uses_fixed_security_boundary(recording_cli, sandbox_request) -> None:
    result = DockerCodingSandboxBackend(command_runner=recording_cli).execute(sandbox_request)
    create = recording_cli.argv_for("create")
    assert "none" == option(create, "--network")
    assert "--read-only" in create
    assert option(create, "--cap-drop") == "ALL"
    assert option(create, "--security-opt") == "no-new-privileges=true"
    assert option(create, "--memory") == str(sandbox_request.memory_bytes)
    assert option(create, "--memory-swap") == str(sandbox_request.memory_bytes)
    assert option(create, "--pids-limit") == str(sandbox_request.max_processes)
    assert result.status == "passed"
    assert not recording_cli.was_called("pull")
    assert not recording_cli.was_called("build")
```

recording CLI 只模拟固定 `image inspect`、`create`、`start --attach`、`inspect` 与 `rm --force` 响应，不启动
真实 Docker。

- [ ] **Step 2: 写资源、脱敏与 cleanup RED 测试**

覆盖：

- daemon/image missing 返回稳定码，不 create；
- host UID 为 0 时 `sandbox_user_invalid`，不启动容器；
- container argv 精确等于 server-owned request argv；
- environment 只有固定 allowlist，不出现 `KEY`、`TOKEN`、`SECRET`、proxy、宿主 HOME；
- mount 只有 scratch -> `/workspace`；
- wall timeout、输出超限、scratch 超限都会 kill；
- OOM inspect 映射 `sandbox_oom_killed`；
- 所有路径都执行带精确 container ID 的 `rm --force`；
- cleanup 失败使成功命令变成 `sandbox_cleanup_failed`；
- stdout/stderr 替换 scratch path、container ID 与 daemon endpoint。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_docker_sandbox.py
```

Expected: FAIL，`assistant_agent.coding.sandbox` 尚不存在。

- [ ] **Step 4: 实现 backend 与可测试 CLI seam**

```python
class CodingSandboxBackend(Protocol):
    def execute(self, request: CodingSandboxRequest) -> CodingSandboxResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

class DockerCommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, timeout: float) -> CompletedProcess[str]:
        raise NotImplementedError

    def popen(self, argv: tuple[str, ...], *, stdout: IO[bytes], stderr: IO[bytes]) -> Popen[bytes]:
        raise NotImplementedError

class DockerCodingSandboxBackend:
    def execute(self, request: CodingSandboxRequest) -> CodingSandboxResult:
        self._require_local_image(request.image)
        container_id: str | None = None
        execution: CodingSandboxResult | None = None
        try:
            container_id = self._create(request)
            execution = self._start_and_collect(container_id, request)
        finally:
            cleanup_status = self._remove(container_id) if container_id else "not_created"
        return self._with_cleanup_status(execution, cleanup_status)
```

Docker create argv 必须由一个纯函数 `_docker_create_argv(request, owner_id, uid, gid)` 生成，末尾仅追加
`request.image` 与 `request.argv`。固定 env 为 `LANG=C.UTF-8`、`LC_ALL=C.UTF-8`、
`HOME=/home/sandbox`、`TMPDIR=/tmp`、`GIT_CONFIG_NOSYSTEM=1`、`GIT_TERMINAL_PROMPT=0`、
`PYTHONDONTWRITEBYTECODE=1`。使用 `--log-driver none`，输出只通过 `docker start --attach` 写入 backend 临时
文件。

watchdog 每 100ms 检查：wall deadline、stdout+stderr、`_tree_bytes(scratch)`。超过限制先执行固定
`docker kill <id>`，再等待 attach process；最终 inspect 后在 `finally` 执行 `docker rm --force <id>`。

`aclose()` 只查询 `label=assistant_agent.coding.owner=<owner_id>` 的容器 ID，逐个固定 argv 删除；拒绝空、带空白
或不符合 Docker ID 形式的返回行。

- [ ] **Step 5: 运行 backend GREEN**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_docker_sandbox.py
```

Expected: PASS。

- [ ] **Step 6: 提交 backend，不提交临时 TDD**

```bash
git add src/assistant_agent/coding/sandbox.py
git commit -m "feat: add docker coding sandbox backend"
```

---

### Task 3: 把 validation 迁入显式 sandbox 执行路径

**Files:**
- Modify: `src/assistant_agent/coding/validation.py`
- Create: `tests/tdd/ai-coding-sandbox/test_sandbox_validation.py`

**Interfaces:**
- Changes: `CodingValidationService(workspace_service, sandbox_backend=None)`
- Consumes: repository `sandbox_enabled` / `sandbox_image`
- Preserves: `run(workspace, repository, *, format_round) -> CodingVerificationResult`
- Preserves: formatter diff -> `workspace_service.validate_patch()` -> independent HITL

- [ ] **Step 1: 写 sandbox selection 与 no-fallback RED 测试**

```python
def test_enabled_repository_uses_only_sandbox_backend(service, sandbox_backend) -> None:
    result = service.run(workspace, sandbox_repository, format_round=0)
    assert result.status == "passed"
    assert sandbox_backend.requests[0].argv == sandbox_repository.commands["test"].argv


def test_sandbox_failure_never_calls_host_executor(service, sandbox_backend, host_executor) -> None:
    sandbox_backend.result = failed_result("sandbox_unavailable")
    result = service.run(workspace, sandbox_repository, format_round=0)
    assert result.error_code == "sandbox_unavailable"
    assert host_executor.calls == []
```

同时覆盖 disabled repository 仍走阶段 2 host executor；enabled 但 backend 未装配返回
`sandbox_unavailable`；sandbox output 正确投影到 `CodingCommandEvidence`。

- [ ] **Step 2: 写 formatter 与 integration gate RED 测试**

构造 sandbox formatter 修改 scratch 的文本文件并返回 passed：第一次产生
`format_approval_required`；批准应用后第二轮无 diff 才 passed；第二轮仍有 diff 返回
`format_not_idempotent`。sandbox 任一失败时 `CodingIntegrationService.create_commit()` 的 recording double 无
调用。

- [ ] **Step 3: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_sandbox_validation.py
```

Expected: FAIL，因为 validation 尚未选择 sandbox backend。

- [ ] **Step 4: 提取 host executor 并接入 backend**

保留 `_execute()` 的阶段 2 语义，但通过私有 `_HostValidationExecutor` 或等价窄函数封装，使选择逻辑只有一处：

```python
if repository.sandbox_enabled:
    if self.sandbox_backend is None:
        return _sandbox_unavailable_evidence(command)
    result = self.sandbox_backend.execute(_sandbox_request(repository, command, scratch))
    evidence = _sandbox_evidence(command, result)
else:
    evidence = _execute_host(command, scratch, temporary)
```

不得把 sandbox 参数加入 Graph state。formatter diff 继续从同一个 disposable scratch 计算并进入既有 validator。

- [ ] **Step 5: 运行 validation GREEN 与阶段 2 定向回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_sandbox_validation.py \
  tests/tdd/ai-coding-validation
```

Expected: PASS。

- [ ] **Step 6: 提交 validation 接入，不提交临时 TDD**

```bash
git add src/assistant_agent/coding/validation.py
git commit -m "feat: execute coding validation in sandbox"
```

---

### Task 4: 接入 Agent Server process owner 生命周期

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Create: `tests/tdd/ai-coding-sandbox/test_sandbox_composition.py`

**Interfaces:**
- Consumes: `CodingConfig.repositories[*].sandbox_enabled`
- Produces: process-owned `DockerCodingSandboxBackend | None`
- Changes: unique `CodingValidationService` receives the same backend instance
- Changes: owner shutdown calls backend `aclose()` exactly through owned service lifecycle

- [ ] **Step 1: 写 composition RED 测试**

```python
def test_owner_constructs_one_backend_for_enabled_repositories(monkeypatch) -> None:
    owner = build_owner(coding_config=config_with_two_sandbox_repositories())
    assert owner.coding_validation_service.sandbox_backend is owner.coding_sandbox_backend


async def test_owner_closes_owned_backend(recording_backend) -> None:
    owner = build_owner(coding_sandbox_backend=recording_backend)
    await owner.aclose()
    assert recording_backend.closed is True
```

同时覆盖：coding disabled 或所有 repository sandbox disabled 时不探测 Docker；backend object 不进入 compiled
graph schema/state；公开 tools 不出现 sandbox/validation/docker Tool。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_sandbox_composition.py
```

Expected: FAIL，因为 owner 尚未装配 backend。

- [ ] **Step 3: 实现唯一 backend composition 与关闭顺序**

owner factory 只在 `coding_config.enabled` 且存在 `sandbox_enabled` repository 时构造 backend。测试通过显式 factory
参数注入 recording backend，生产默认使用 `DockerCodingSandboxBackend()`。关闭顺序必须先阻止新 validation，
再等待/关闭 validation，最后由 backend 清理自身 label 的遗留容器；清理异常记录但不能跳过其他 owner 资源关闭。

- [ ] **Step 4: 运行 composition GREEN 与 runtime core 定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox/test_sandbox_composition.py \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS；core 测试数量不因具体 sandbox feature 增加。

- [ ] **Step 5: 提交 composition，不提交临时 TDD**

```bash
git add src/assistant_agent/agent_server/services.py
git commit -m "feat: compose coding sandbox lifecycle"
```

---

### Task 5: 同步 authority 并完成阶段 4A 验收

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Documents: Docker backend owner、digest image、容器安全策略、no-fallback、阶段 4A 非目标
- Documents: sandbox/validation 不属于模型 Tool
- Routes: `src/assistant_agent/coding/sandbox.py` 与 `tests/tdd/ai-coding-sandbox`

- [ ] **Step 1: 更新 authority contract 正文与 manifest**

Agent Server authority 记录：唯一 process-owned backend；repository opt-in；本地 digest image；默认断网；容器
不进入 checkpoint；cleanup label；sandbox enabled 时 no-fallback。Tool authority 只补充 sandbox command 不是
`BaseTool`，固定 argv 仍由 validation service 决定。manifest 的 agent-server source globs 增加
`src/assistant_agent/coding/sandbox.py`，verification 增加阶段 4A TDD 命令。

- [ ] **Step 2: 运行阶段 4A 全部临时 TDD**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-sandbox
```

Expected: PASS。

- [ ] **Step 3: 运行阶段 2/3 与受影响 core 最小回归**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/ai-coding-validation \
  tests/tdd/ai-coding-integration \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS。若 worktree 中看不到主仓库 ignored TDD，合并前分别在其所在工作区执行并记录；不得复制后提交。

- [ ] **Step 4: 运行 authority validator**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: `valid=true`、`errors=[]`、`review_required=[]`。

- [ ] **Step 5: 执行真实 Docker 离线 smoke（只使用本地镜像）**

先只读枚举本地 RepoDigest：

```bash
docker image ls --digests --format '{{.Repository}}@{{.Digest}}' \
  | awk '$0 ~ /@sha256:[0-9a-f]{64}$/ {print; exit}'
```

若存在本地 digest image，以临时 repository config 显式运行 sandbox smoke，验证：`network none`、UID 非 0、
rootfs 写入失败、`/workspace` 写入成功、timeout/resource error 结构化、测试结束无 owner-label 容器。若不存在，
记录 `unconfigured: no local digest-pinned image`；禁止 pull/build 或联网补齐。

- [ ] **Step 6: 等待现有 8089 hot reload 并只读检查健康**

```bash
sleep 3
curl -fsS --max-time 10 http://127.0.0.1:8089/ok
```

Expected: `{"ok":true}`。不操作服务进程。

- [ ] **Step 7: 提交 authority 文档**

```bash
git add docs/agent-server-architecture.md docs/tool-calling-architecture.md docs/authority.toml
git commit -m "docs: document coding sandbox isolation"
```

---

## 完成汇报格式

```text
完成：AI Coding 阶段 4A 本地容器强隔离。
Core invariant: unchanged；Graph 路由、HITL 与 integration 顺序未改变。
Tests: added tests/tdd/ai-coding-sandbox for temporary RED/GREEN; user may delete this directory manually.
Validation: <实际命令与结果>。
Docker smoke: <本地 digest image 与离线验证结果，或明确 unconfigured>。
Provider: 未调用真实 Provider；全部 pytest 使用 mock/offline。
Limitations: 无联网、依赖安装、secret 注入、push/PR；需要 operator 预置协议合规的 digest-pinned image。
```

---

### Task 6: 安全审查后重构 trusted runner 与硬配额数据面

**Files:**
- Modify: `src/assistant_agent/coding/config.py`
- Modify: `src/assistant_agent/coding/models.py`
- Rewrite: `src/assistant_agent/coding/sandbox.py`
- Create: `src/assistant_agent/coding/sandbox_runner.py`
- Modify: `src/assistant_agent/coding/validation.py`
- Modify: `tests/tdd/ai-coding-sandbox/*`
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/tool-calling-architecture.md`

**Required corrections:**

- [ ] RED：证明输入只在启动前通过受控 `docker cp` 进入只读 rootfs，运行时无 host mount，且
  `/workspace` 为 size/inode-bounded tmpfs。
- [ ] RED：证明 runner 输出突发超限不会创建无界 host file/buffer，并终止 command。
- [ ] RED：证明固定 container name/hostname 在 create/attach 异常时仍确定性清理且不泄露 ID。
- [ ] RED：证明 attach 非零或 inspect `Running=true` 绝不映射 passed。
- [ ] RED：证明 formatter 只通过有界 UTF-8 `formatter_files` 返回并复用 host Git baseline/patch HITL。
- [ ] RED：证明 cleanup/timed-out/OOM facts 投影到 verification evidence，失败时 integration 不可达。
- [ ] GREEN：实现 image protocol label、runner、bounded pipe protocol、tmpfs bytes/inodes 与 no-fallback。
- [ ] 验证：阶段 4A TDD、阶段 2/3 回归、完整 core、authority validator 和 compliant-image Docker smoke。
- [ ] 复审：Critical/Important findings 清零后才提供 merge 选项。

Task 6 覆盖前文中 RW bind `/workspace`、host output file、host tree polling 和普通 image smoke 的旧步骤。完成汇报
中的限制改为：无联网、依赖安装、secret 注入、push/PR；真实 smoke 需要 operator 预置 compliant digest image。
