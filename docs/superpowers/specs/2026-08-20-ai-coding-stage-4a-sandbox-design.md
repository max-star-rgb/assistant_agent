# AI Coding 阶段 4A 本地容器强隔离设计

日期：2026-08-20

## 1. 目标

在阶段 2 的受控验证闭环与阶段 3 的受控 Git integration 之前，引入本地 Docker 容器执行边界。所有需要
执行代码的 test、lint、format、build 命令在短生命周期容器中运行，默认断网、不注入宿主秘密，并受
CPU、内存、进程、磁盘、输出和墙钟时间限制。

阶段 4A 保持 `AssistantRootGraph -> AssistantCodingGraph` 拓扑、既有 formatter digest-bound HITL 以及
commit/merge 顺序不变。它只替换 validation service 的执行 backend，不增加模型可见 Tool，也不引入第二套
run/checkpoint 生命周期。

## 2. 范围与非目标

阶段 4A 提供：

- repository 级、默认关闭的 Docker sandbox 配置；
- digest 固定且已在本机预置的 repository sandbox image；
- 可替换的 `CodingSandboxBackend` 协议和首个 Docker CLI 实现；
- 默认断网、非 root、只读根文件系统、capability 收缩和默认 seccomp；
- 每条验证命令独立容器及确定性清理；
- 结构化 sandbox evidence 和稳定失败码；
- sandbox 启用后禁止静默回退宿主 subprocess。

阶段 4A 不提供：

- 网络、代理、egress allowlist 或 DNS；
- package manager 联网安装依赖；
- API key、token、SSH agent、云身份或其他秘密注入；
- 自动 pull、build、更新或签名验证镜像；
- 客户端或模型提交 image、argv、environment、mount、user、network 或 Docker 参数；
- push、PR、remote fetch/pull、部署或冲突自动修复；
- 把普通 OCI 容器描述为虚拟机级隔离。

网络、依赖安装、外部资源和临时凭据属于后续阶段 4B，必须使用独立审批与 egress 治理，不能通过放宽
阶段 4A 参数提前实现。

## 3. 威胁模型

### 3.1 不受信对象

- repository 中被验证的源码、测试、构建脚本和 formatter；
- validation command 启动后的全部子进程；
- 命令输出、生成文件和退出状态。

### 3.2 受信对象

- Agent Server process owner 与本机 Docker daemon；
- 服务端 repository allowlist、固定 command 配置和 digest-pinned image 配置；
- operator 预置的镜像内容；
- `CodingWorkspaceService`、`CodingValidationService` 与 sandbox backend 实现。

### 3.3 防护边界

阶段 4A 防止不受信代码直接读取 source repository、coding worktree、Agent Server cwd、宿主环境变量和宿主
秘密；禁止容器主动联网，并限制资源消耗和宿主可写路径。它不防御恶意 Docker daemon、恶意 operator、
内核/容器运行时逃逸或已被篡改但仍使用同一 digest 的镜像存储。Docker daemon 访问本身是高权限能力，
因此 backend 不暴露为模型 Tool，容器内也绝不挂载 Docker socket。

## 4. 架构与职责

### 4.1 稳定协议

新增 `src/assistant_agent/coding/sandbox.py`：

```python
class CodingSandboxBackend(Protocol):
    def execute(
        self,
        request: CodingSandboxRequest,
    ) -> CodingSandboxResult:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
```

`CodingSandboxRequest` 只包含服务端已经决定的事实：

- digest-pinned image；
- 固定 argv；
- host scratch path 与固定 container workspace `/workspace`；
- wall timeout、CPU、memory、PID、单文件、总磁盘和输出限制；
- command ID、kind 与无秘密的审计关联引用。

`CodingSandboxResult` 只返回稳定结构化事实：

- status、exit code、duration；
- stdout/stderr 的有界投影及完整输出 digest；
- timed out、OOM killed、resource exceeded；
- 稳定 error code。
- cleanup status：未创建容器为 `not_created`，成功删除为 `removed`，删除失败为 `failed`。

协议不返回 Docker client、container object、host path 或原始 daemon stderr。未来远程 sandbox 通过新增 adapter
实现同一协议，不改变 `CodingValidationService` 或 CodingGraph。

### 4.2 Docker CLI backend

`DockerCodingSandboxBackend` 使用固定 argv 调用本机 Docker CLI，不使用 shell，不引入 Docker Python SDK。
backend 负责：

1. 使用 `docker image inspect` 验证精确 digest image 已在本机存在，禁止 pull/build；
2. 创建带 owner/run labels 的短生命周期容器；
3. 启动、等待、超时终止并读取有界日志；
4. 使用 `docker inspect` 区分普通失败、OOM 与运行时错误；
5. 在所有成功和失败路径删除容器；
6. `aclose()` 仅清理由本 process owner label 创建的遗留容器。

container ID 是 backend 内部的短生命周期 sandbox ref，不写入 Graph state/checkpoint，也不出现在模型或客户端
可见结果中。

### 4.3 Composition

`AgentServerExecutionOwner` 在 coding 启用且至少一个 repository 开启 sandbox 时构造唯一 Docker backend，
并把它注入唯一 `CodingValidationService`。owner 关闭时先关闭 validation service，再执行 backend 的有界遗留
容器清理。

当 repository `sandbox_enabled=false` 时，保留阶段 2 的宿主受限 subprocess 兼容路径。当
`sandbox_enabled=true` 时，任何 Docker/image/runtime 错误都 fail closed，绝不回退宿主执行。

## 5. 配置契约

`CodingRepositoryConfig` 增加：

```python
sandbox_enabled: bool = False
sandbox_image: str | None = None
```

约束：

- `sandbox_enabled=true` 必须同时配置非空 `verification_sequence`；
- `sandbox_image` 必须符合 `<repository>@sha256:<64 lowercase hex>`，拒绝 tag-only、短 digest、latest 和空值；
- 配置解析不 pull/build 镜像；实际执行前只做本地 inspect；
- integration 可以与 sandbox 分别启用，但 sandbox 验证失败时 integration 永远不可达；
- 公开 `AssistantRootInput`、Runtime Context、messages、Tool schema 和 resume payload 不增加 sandbox 参数。

阶段 4A 复用 `CodingCommandConfig` 的 timeout、CPU seconds、memory、process、output 和 disk limits，并增加
`cpu_cores` 作为 Docker CFS quota，取值范围 `0.1..16.0`，默认 `1.0`。所有值均由服务端配置，模型不可修改。

## 6. 容器安全策略

每次执行至少使用以下确定性策略：

```text
--network none
--read-only
--user <agent-server-uid>:<agent-server-gid>
--cap-drop ALL
--security-opt no-new-privileges
--memory <memory_bytes>
--memory-swap <memory_bytes>
--cpus <cpu_cores>
--pids-limit <max_processes>
--ulimit cpu=<cpu_seconds>:<cpu_seconds>
--ulimit fsize=<max_file_bytes>:<max_file_bytes>
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=<bounded>
--tmpfs /home/sandbox:rw,noexec,nosuid,nodev,size=<bounded>
--mount type=bind,src=<scratch>,dst=/workspace,rw
--workdir /workspace
```

同时满足：

- 不使用 `--privileged`、`--device`、`--cap-add`、host network、额外 group 或 Docker socket；
- 不覆盖 Docker 默认 seccomp 为 unconfined；
- 不继承 Agent Server environment；只注入固定 locale、HOME、TMPDIR、Git non-interactive 和
  `PYTHONDONTWRITEBYTECODE`；
- scratch 是 stage 2 已有的一次性副本，唯一宿主读写 mount；source repository、coding worktree 和 Agent
  Server cwd 均不挂载；
- 后台监测 scratch 总字节，超过 `max_disk_bytes` 立即 kill，结束后再次扫描；单文件写入同时受 file-size
  ulimit；
- wall timeout 后先 kill 整个容器，再等待确认终止，最后清理；
- 输出先落入 backend 管理的有界文件，再计算完整 digest 和脱敏投影。

Docker bind mount 不提供跨存储驱动一致的硬总量 quota。阶段 4A 使用 file-size hard limit、100ms 周期总量监测
和执行后扫描组合约束总磁盘量；验收不宣称零字节瞬时超调。需要内核级严格总量 quota 时，应在后续使用
受控 tmpfs/volume runner 或远程 sandbox backend，不得伪称当前 bind mount 已提供该保证。

安全参数依据 Docker 官方的 [resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)、
[none network driver](https://docs.docker.com/engine/network/drivers/none/)、
[runtime privilege and capabilities](https://docs.docker.com/engine/containers/run/) 和
[seccomp profile](https://docs.docker.com/engine/security/seccomp/) 契约。

## 7. Validation 数据流

```text
approved patch
  -> CodingValidationService 创建一次性 scratch
  -> 复制 workspace，初始化 formatter diff 基线
  -> 构造 CodingSandboxRequest
  -> DockerCodingSandboxBackend 执行固定 argv
  -> CodingSandboxResult 投影为 CodingCommandEvidence
  -> 非 passed: validation failed，停止 integration
  -> passed formatter with diff: 复用 workspace validate_patch
  -> formatter digest-bound HITL
  -> 再次 sandbox validation
  -> controlled commit/merge
```

Graph 不保存 scratch path、container ID 或 backend object。formatter 仍只能把 sandbox scratch 中产生的文本 diff
交给阶段 1 validator；第二轮仍产生 diff 时继续按阶段 2 规则 `format_not_idempotent` fail closed。

## 8. 失败与审计

新增稳定失败码：

- `sandbox_unavailable`
- `sandbox_image_missing`
- `sandbox_image_mismatch`
- `sandbox_create_failed`
- `sandbox_start_failed`
- `sandbox_timeout`
- `sandbox_oom_killed`
- `sandbox_resource_exceeded`
- `sandbox_output_invalid`
- `sandbox_cleanup_failed`

失败优先级：cleanup failure 不覆盖原始 execution failure，但必须作为高严重度结构化 audit fact；若 execution
成功而 cleanup 失败，整个 command 仍失败，禁止 commit/merge。任何错误投影都不得包含 host path、container
ID、完整 image metadata、Docker daemon 地址、原始 daemon stderr、秘密或完整源码。

审计只记录 identity reference、thread/run、workspace ref、repo ID、command ID、image digest、资源策略摘要、
开始/结束时间、结构化 status/error code、exit code、输出 digest 和 cleanup status。

## 9. 测试与验证策略

新增临时 `tests/tdd/ai-coding-sandbox/`，不加入默认 pytest、不自动晋升 core，用户可在完成后手动整目录
删除。

临时 RED/GREEN 覆盖：

1. 配置拒绝 tag、短 digest、未配置 image 和无 verification sequence；
2. recording runtime 收到完整且不可放宽的 security options；
3. image 只 inspect、不 pull/build；
4. 模型、客户端、Context 和 resume schema 不出现 sandbox override；
5. timeout、OOM、PID/memory/CPU/disk/output 和 cleanup failure 均 fail closed；
6. 容器只能 mount scratch，环境不包含 key、token、proxy 或宿主 HOME；
7. sandbox enabled 时无 fallback，disabled 时阶段 2 行为保持兼容；
8. formatter diff 继续进入既有 validator 与独立 HITL；
9. validation 失败时阶段 3 commit/merge 节点不可达；
10. composition 只构造并关闭一份 backend。

真实 Docker smoke 使用本机已经存在的 digest-pinned image，显式 offline 运行，验证网络不可达、非 root、
rootfs 不可写、scratch 可写、资源限制和容器清理。smoke 不自动 pull 镜像，不进入默认 pytest；本机没有满足
条件的镜像时明确报告 skipped/unconfigured，而不是联网补齐。

`Core invariant: unchanged`。`LOOP-001` 与 `CTX-001` 的可观察 Graph/HITL 契约不变，因此不修改
`tests/core`。若实现中实际改变 Graph 路由、interrupt schema 或 integration 顺序，必须停止并重新评估对应
invariant，而不能用本设计预先授权。

## 10. Authority 同步

- `docs/agent-server-architecture.md`：记录 process-owned sandbox backend、容器生命周期、默认断网和禁止
  fallback；
- `docs/authority.toml`：把 `src/assistant_agent/coding/sandbox.py` 与阶段 4A TDD 路由到 agent-server；
- `docs/tool-calling-architecture.md`：仅在需要明确 sandbox 不是模型 Tool 时补充边界；
- `.env.example`：给出 disabled-by-default、digest-pinned repository JSON 示例，不提供真实镜像或秘密。

## 11. 验收标准

1. sandbox disabled 时现有阶段 2/3 行为不变。
2. sandbox enabled 时所有 validation command 只在 Docker 容器内运行，宿主 subprocess executor 不可达。
3. image 必须本地存在且 digest 精确匹配；任何缺失或 daemon 错误都不 pull、不 fallback。
4. 容器无网络、非 root、只读 rootfs、无 capabilities、启用 no-new-privileges 与默认 seccomp。
5. 容器只能写一次性 scratch 与有界 tmpfs，不能看到 source repo、coding worktree、宿主 cwd、Docker socket
   或宿主秘密。
6. timeout、OOM、资源超限、异常退出和 cleanup failure 都产生结构化失败并阻止 commit/merge。
7. formatter 结果仍经过既有 patch validator、digest-bound HITL 和最多一轮规则。
8. container ID、host path、原始 Docker stderr 和 backend object 不进入 checkpoint 或模型可见结果。
9. owner 正常关闭时只清理由自身 label 标记的遗留容器，不影响其他容器。
10. 临时 TDD、阶段 2/3 最小回归、authority validator 与现有 8089 hot reload 健康检查通过。

## 12. 后续阶段 4B

阶段 4B 在独立设计和审批后才可增加：

- network profile 与域名/IP/端口 egress allowlist；
- package install intent、lockfile 绑定和独立 HITL；
- 短生命周期 credential broker 与最小 scope secret mount；
- artifact ingress/egress 扫描、大小限制和 provenance；
- 远程 sandbox adapter、租约、重连和 provider-side quota。

阶段 4B 不得通过为 Docker backend 增加任意 flags、环境变量或 host mount 绕过本阶段协议。

## 13. 安全审查修订：trusted runner 与 tmpfs 数据面

独立安全审查证明普通 RW bind scratch 与宿主输出文件不能满足本规格的强隔离声明：突发 stdout/stderr 可在
轮询间隔内耗尽宿主资源，目录扫描不能提供 inode hard quota，也可能被权限变化和海量目录项拖延。因此本节
覆盖第 6、7 节中与 RW `/workspace` bind mount、宿主输出文件和轮询磁盘扫描冲突的描述。

修订后的唯一执行数据面：

```text
host disposable scratch --docker cp before start--> image /input
Docker tmpfs(size + nr_inodes) -----------> /workspace
trusted image runner ---------------------> fixed command argv
runner bounded JSON protocol ------------> backend bounded pipe reader
validated formatter files ---------------> host scratch -> existing git diff/validator/HITL
```

- digest-pinned image 必须声明 label `org.assistant-agent.coding-sandbox-protocol=1`，并包含固定入口
  `/usr/local/bin/assistant-agent-sandbox-runner`；inspect 同时验证 RepoDigest、label 和入口存在所需的 image
  contract，不满足时返回 `sandbox_image_mismatch`。
- backend 预生成随机但有界的 container name，并固定 `--name` 与 `--hostname sandbox`。create 超时、stdout
  缺失或格式错误时仍按预生成 name 执行 `docker rm --force`，不能依赖 daemon 返回 container ID。
- 容器启动前使用固定 `docker cp --archive <scratch>/. <name>:/input` 将已受 workspace policy 约束的
  disposable scratch 复制到镜像内 `/input`；运行时 rootfs 只读，不存在 host bind mount，也不向不受信命令
  暴露宿主路径。`/workspace` 是 `tmpfs`，同时配置 `size=<max_disk_bytes>` 与带余量的 inode hard limit。
  不再递归轮询 host scratch，也不允许不受信命令直接写任何 host mount。
- trusted runner 先把 `/input` 的普通文件复制到 `/workspace`，拒绝 symlink、device、FIFO、socket、单文件
  超限、总字节超限和 inode 超限；随后以固定 cwd/env、无 shell启动 command。
- runner 使用流式 reader 计算 stdout/stderr digest，只保存总计 `max_output_bytes` 的有界投影；超限立即终止
  command process group并返回 `sandbox_resource_exceeded`。backend 对 runner 自身 stdout/stderr 再实施独立
  hard byte bound，绝不调用 `read_bytes()` 或 `communicate()` 收集无界内容。
- runner 的 JSON protocol 只包含 status、exit、duration、output digest/投影，以及 formatter 成功时的有界
  `formatter_files`。formatter 文件只能是相对 UTF-8 普通文件，数量不超过 `max_changed_files`，单文件与总
  payload 不超过既有 patch/file 限制；删除、symlink 和特殊文件直接失败。
- backend 验证完整 protocol 后，validation service 才把 formatter 文件物化到未被容器写过的 host scratch，
  再调用既有 `_formatter_diff()` 与 `CodingWorkspaceService.validate_patch()`。容器内 `.git` 即使被修改也不影响
  host 基线。
- backend 必须验证 attach CLI return code 与 inspect state 的 `Running=false`、`ExitCode`、`OOMKilled`；任一
  不一致都返回 `sandbox_start_failed`，不得报告 passed。
- cleanup failure 始终保留为结构化 `cleanup_status=failed`；validation evidence 增加 cleanup/timed-out/OOM
  facts，使“execution failure + cleanup failure”仍可审计并阻止 integration。

修订后不再接受“100ms host tree scan 存在瞬时超调”作为阶段 4A 限制；bytes 与 inode 限制由 Docker tmpfs
硬执行。真实 Docker smoke 必须使用符合 protocol label/runner contract 的本地 digest image，否则明确
`unconfigured`，不得用普通 image 绕过 runner。
