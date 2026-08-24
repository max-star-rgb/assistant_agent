# LangGraph Agent Server 部署架构

最后更新：2026-08-24

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 生产 Assistant Graph 的 Agent Server 部署、身份和资源生命周期权威 |
| Owns | auth、assistant、thread、run、queue、checkpoint、Store、cancel、stream 与 custom route 装配 |
| Does not own | Assistant 节点推理、Tool schema、Memory 后端语义、Media-Agent wire 字段 |
| 源码与 schema 入口 | `langgraph.json`、`src/assistant_agent/agent_server/` |
| 验证入口 | `docs/authority.toml` 中 `agent-server.verification` |
| 相邻 authority | 媒体 wire 见 [`media-agent-service-websocket.md`](media-agent-service-websocket.md)；运行图见 [`runtime-event-stream-architecture.md`](runtime-event-stream-architecture.md)；视觉流水线见 [`visual-perception-architecture.md`](visual-perception-architecture.md) |

## 生产 Graph 入口

Agent Server process owner 静态持有一份 `CodingWorkspaceService`、一份 `CodingValidationService` 和一份
`CodingIntegrationService`。coding 默认 disabled；显式启用后，
source repository 只能从服务端 JSON allowlist 通过 opaque `coding_repo_id` 选择。每个
`user.identity + thread_id + repo_id` 解析到独立临时 Git worktree，workspace ref 使用服务端 HMAC 派生，
metadata、锁和 TTL 位于受管 workspace root，不进入 Graph state。

每个 repository 的 `parallel_analysis_enabled` 也是服务端静态配置且默认关闭。显式启用时，同一 process-owned
`CodingWorkspaceService` 在首次 draft 前创建内容寻址、只读、identity/thread/workspace 绑定的 analysis snapshot；
snapshot 覆盖创建时允许访问的已跟踪修改和新增文本文件，不修改真实 worktree 或 Git index。三个原生 `Send`
worker 只借用 snapshot-bound read 接口。join 后 owner 释放 active lease；释放清理失败只登记
`cleanup_pending`，已释放 snapshot 仍保留到 TTL，并由 workspace owner 的受管 reaper 清理过期、构建残留或隔离
异常目录。process owner 在 coding 启用时启动唯一有界周期 reaper，每轮只从增量 `scandir` cursor 读取配置上限内的
management root/snapshot entry，不预收集或排序完整目录；root traversal 与每个 workspace 的固定大小 directory cookie
保证跨任意数量 workspace 的有界轮转，目录消失与 shutdown 都会关闭并清除对应句柄。该周期入口只删除
`analysis-snapshots` 下的受管目录，不调用 Git、不删除 workspace repo，也不读取或修改 `.git/worktrees` admin metadata。
workspace TTL 仍沿用 resolve 触发的既有 `cleanup_expired()` 生命周期。`aclose` 会取消该 task 并执行一次安全有界的
snapshot-only 清理；它不创建 Graph、run 或第二套 Runtime。pending checkpoint
恢复严格校验既有物理 snapshot，过期、身份或 digest 不匹配时不静默重建；join 后的 approval/repair resume 只校验
checkpoint contract 与 workspace/base，不要求 released snapshot 仍在物理 TTL 内，因此 snapshot 不是 mutation gate。
final coding review 复用同一个 process-owned snapshot service、lease、TTL 与 snapshot-only reaper，不创建 review
专用目录 owner 或 workspace reaper。pending review 只接受 active v2 immutable manifest；completed canonical report
可在原物理 snapshot 缺失/自然过期后由 Graph 重新创建 fresh snapshot 做内容身份比较。该兼容不允许 pending v1、
completed v2 schema downgrade 或 metadata/manifest mismatch；真正历史 completed v1 只按 checkpoint 中的 legacy
schema/digest binding fresh-rebind。周期 owner 与 `aclose` 仍只删除受管 `analysis-snapshots` 子树，不触碰 workspace
repo、Git admin metadata，不运行 reviewer、自动修复、commit 或 merge。validation 未成功交接、workspace comparison
变化、review-off terminal 和 commit comparison 退出都由当前 Graph/service owner 确定性、幂等 release snapshot；review
approve 且 integration 开启时 lease 必须保持 active 到 commit checkpoint，不能依赖 TTL 或扩大 periodic owner 收口。

所有 workspace、snapshot 与 integration Git 子进程都设置 `GIT_NO_LAZY_FETCH=1`、关闭 credential prompt 和 system/
global config；partial/promisor repository 缺少本地对象时稳定 fail closed，不允许 Git 隐式 lazy fetch。analysis diff
在临时 bare `GIT_DIR` 中只通过受管 object directory/alternate 读取两棵 tree，隔离 source repository 的 local config 与
`info/attributes`；同时固定 full object index、Myers、order file、inter-hunk context、quotePath、prefix、rename、
textconv 与 external-diff 参数，因此同一 baseline/current tree 的 digest 只由 tree object 与固定协议决定。snapshot
scanner 使用增量 `scandir`，每取得一个 entry 就以 O(1) 累计 visited entry、directory、attempted/read bytes 与
included file/bytes 硬限制；不会在预算前收集或排序单目录全部 entry。protected、non-UTF-8、unreadable 和 oversize
entry 也消耗扫描预算，Git baseline index 输出同样按 scan entry/byte 上限约束，不能用最终被排除的文件绕过资源上限。

Graph checkpoint 只保存 opaque workspace ref、base commit、proposal/validation、结构化 repair
failure evidence/history/digests、opaque analysis snapshot contract、有界规范化 analysis result/status 和结构化结果，
不保存完整命令日志、snapshot 或 workspace 宿主路径、Git client/process、backend client、文件句柄或进程对象。
analysis worker transcript 与临时 task/context 不进入主对话 `messages`；repair evidence 仅含有界命令投影及其
digest，模型使用的临时 context 也不写入对话
`messages`。interrupt/resume 由 Agent Server/LangGraph 原生所有；resume 时 backend 重新校验唯一认证
身份、thread、base commit、目标文件 digest、patch digest 与 repair 累计 diff digests，项目不保存
平行 resume 机制。analysis 的 `partial|unavailable` 只降低 advisory evidence 质量；repair、patch、dependency、
credential、artifact 与 merge approval resume 不重跑已完成分析，所有 mutation 仍由同一顺序治理 lane 执行。
integration 默认关闭，且只在最终一轮完整 validation gates 通过后才能进入；
关闭时终态保留 worktree 到 TTL，不 commit、merge、push 或写回 source repository。
final review checkpoint 还保存 expected snapshot schema version、opaque final snapshot、input、固定 task inventory、
signed bounded results、canonical report/status、validation digest、decision binding 与 audit decision，不保存 reviewer
transcript、Provider client、文件句柄或宿主 snapshot path。`CodingRepositoryConfig.code_review_enabled` 是受信
repository-static 的 default-off capability；启用后即使 integration 关闭也必须经过独立 review decision，`unavailable` 只描述 advisory
review 质量，不产生自动批准或自动修复。

repository 可独立显式启用本地 Docker sandbox。Agent Server process owner 只构造一份
`DockerCodingSandboxBackend` 并注入唯一 validation service；backend 与 container ID 不进入 Graph state 或
checkpoint。sandbox image 必须由 operator 预置并固定到完整 RepoDigest，声明
`org.assistant-agent.coding-sandbox-protocol=1`，并提供固定 trusted runner；runtime 只执行本地 inspect，不
pull、build 或更新镜像。每条固定 validation command 使用预生成 name 的独立短生命周期容器，hostname 固定为
`sandbox`，默认 `network none`、非 root、只读 rootfs、drop all capabilities、no-new-privileges、默认
seccomp，并限制 memory、CPU、PID、file/output 和 wall time。容器启动前，宿主 scratch 通过受控
`docker cp --archive` 复制到镜像内 `/input`；运行时只读 rootfs 使输入不可写，且不向容器暴露宿主路径。
执行目录 `/workspace` 是具有 size 与 inode 硬限制的 tmpfs；runner 通过有界 JSON pipe 返回命令输出与 formatter 文件，
宿主再将合规 formatter 结果物化到未被容器写入的 scratch。容器不挂载 source repository、coding worktree、
Agent Server cwd、Docker socket 或宿主秘密。sandbox 启用后任何 daemon、image、protocol、attach state、
resource 或 cleanup 错误都 fail closed，禁止回退宿主 subprocess；owner 关闭时只清理由自身 label 标记的
遗留容器。

Stage 4B1 的 dependency profile 仍是 repository 静态 allowlist。lockfile 变化经独立 digest-bound HITL 获批后，
进程 owner 才创建短生命周期 downloader、Docker internal network 和双网卡 allowlist proxy。downloader 没有
直接外部路由，proxy 只允许配置中的 exact public FQDN + HTTPS 443，并拒绝特殊地址解析结果；两类镜像都必须
operator 预置、RepoDigest pinned 且声明对应协议。下载结果只允许 hash-pinned binary wheel，导出后验证类型、
名称、版本、数量、大小和 SHA-256 provenance。验证容器仍保持 `network none`，启动前复制 wheelhouse 并由
trusted runner 固定离线安装。container、network、artifact 和 cleanup 对象不进入 checkpoint；任何不确定状态
都阻止 validation、controlled commit 与 merge，禁止回退宿主 pip 或普通联网容器。

Stage 4B2 只为上述 Python hash-pinned wheel 路径增加 operator-owned 私有 registry profile。当前 tokenless
developer identity 不用于选择凭据；repository 只能引用服务端已配置的 opaque credential profile。依赖审批后还需
独立 `coding_credential_lease` HITL，审批绑定 registry scope、TTL、dependency plan/policy 与 credential policy
digest，恢复时全部重新计算。Graph state、checkpoint、interrupt 和 evidence 均不保存 secret、secret env 名、
Authorization header 或原始 lease ID。

进程内 `EnvironmentCredentialBroker` 仅在审批通过后读取专用
`MULTIMODAL_AGENT_CODING_CREDENTIAL_*` 环境变量，产生不可序列化、显式归零的短时 lease。私有下载不使用普通
CONNECT proxy 注入 TLS header，而使用 operator 预置、RepoDigest pinned 且声明
`org.assistant-agent.coding-registry-gateway-protocol=1` 的反向 gateway。secret 只通过
`docker exec -i` stdin 以有界二进制 envelope 发送给固定 credential loader；envelope 绑定 request/policy digest、
registry scope、lease ID digest 和 deadline，并写入 gateway 独占 tmpfs。明文输入在注入后立即归零，不得进入 argv、container env、
bind mount、`docker cp`、日志或 downloader。gateway readiness 成功后 downloader 才能启动，downloader 仅访问
internal network 上的 gateway；整个下载受 monotonic lease deadline 限制。下载结束或异常退出时先调用固定 loader
revoke，删除失败再 kill/retry；acquire、inject、revoke 的脱敏状态进入 evidence。lease、gateway、network、
container 或 cleanup 任一状态不确定即 fail closed。
当前 broker 对静态 operator token 只提供进程内最小暴露与 TTL 使用边界，不宣称具备上游可撤销的真实临时凭据语义。

Stage 4B3 增加 repository 静态 artifact profile，但不接受任意 URL、上传内容、发布目标或私有 artifact
凭据。ingress 只在严格 JSON manifest 发生变化时生成独立 digest-bound HITL；manifest 中每个 HTTPS URL
必须命中 exact public FQDN + 443，并同时固定 filename、media type、size 与 SHA-256。恢复后重新计算 plan，审批
漂移立即拒绝。获批后进程 owner 使用 operator 预置且 RepoDigest pinned 的 fetcher、allowlist proxy 与 scanner：
fetcher 只能经 internal network 访问 proxy，scanner 固定 `network none`；抓取、host-side exact hash/size 校验、
恶意内容扫描或 cleanup 任一失败均 fail closed。清洁 ingress 仅通过 `docker cp --archive` 复制到验证容器
`/artifacts/input`，runner 只暴露固定 `ASSISTANT_AGENT_ARTIFACT_ROOT`，验证容器仍保持 `network none`，不挂载
宿主目录。

build output 只有 artifact profile 中按 command ID 声明的精确相对路径可导出。trusted runner 在成功 build 后拒绝
缺失文件、目录、symlink、hardlink、超限文件或重复 export，并返回 size/SHA-256 元数据；sandbox backend 在容器
停止后、删除前只复制这些精确路径，再在宿主重新校验。导出结果必须再次进入 RepoDigest-pinned scanner 的
`network none` 容器，扫描通过后才原子写入受管 artifact bundle root。上层 evidence 只保存 scanner/manifest
digest、状态和不可解释的 `artifact_bundle_*` 引用，不保存宿主路径或二进制；bundle metadata 带服务端 TTL。
当前阶段不提供下载/发布 route，不把 bundle 自动写回 source repository，也不允许 validation 网络访问。

每个 repository 可在同一服务端 allowlist 中配置有序 `verification_sequence`，其中 command ID 只映射到
受信固定 argv、command kind 和资源上限。验证进程不在 source repo、Agent Server cwd 或受管 worktree 中
直接启动，而在 workspace root 下的一次性 scratch 副本中使用固定 cwd、净化环境、wall timeout、进程组
终止、POSIX CPU/内存/进程/文件限制和总磁盘扫描；stdout/stderr 只保留有界投影和 digest。scratch 与进程
对象不进入 checkpoint，命令结束后无条件清理。此宿主限制不是容器级恶意代码或网络隔离；强 sandbox、
依赖安装和 egress 控制仍属于后续阶段。

repository 只有显式 `integration_enabled=true` 且 verification sequence 非空时，才允许在验证通过后进入受控
Git integration。controlled commit 只在 thread-scoped detached worktree 中通过临时 index 与 `commit-tree`
创建，author/committer 由服务端配置，Git hooks、signing、credential prompt 与 system config 禁用。目标
preflight 要求配置 path 当前 checkout 精确等于 target branch、HEAD 冻结且 worktree/index clean。非 FF 情况
先用 `merge-tree --write-tree` 和 `commit-tree` 在 object database 中预生成双亲 result commit；preview 与
source/target/result commit 进入 checkpoint，但宿主 path、Git process 和 stderr 不进入。独立 merge HITL 后
最终目标写入只执行 `merge --ff-only <result_commit>`；HEAD 漂移、dirty、冲突或 preview 不匹配均停止且不
重算。阶段 3 不 fetch/pull/push、不创建 PR、不使用远程凭据，也不自动修复冲突。
当 final review 显式启用时，controlled commit 还必须位于 canonical report 的独立 user approve 之后；review reject、
binding mismatch 或未完成 review 都不能调用 commit service。integration 关闭时 review approve 只产生 applied
terminal，不隐式启用 Git integration。

`langgraph.json` 只注册当前两张原生 Graph：

```text
assistant-native-v3 -> assistant_agent.agent_server.graph:native_assistant_graph
assistant-memory-v1 -> assistant_agent.agent_server.graph:native_memory_graph
```

`assistant-native-v1` 与 `assistant-native-v2` 都不作为指向当前图的 alias 注册：当前图不能解释或 replay
v1/v2 planning checkpoint。这是一次显式 graph ID 升级，不是 checkpoint 自动 migration。新原生 Deep Agents
planning state 也不迁移同一 v3 graph ID 下已删除的 A-lite planning state；切换后既有 planning thread 只作历史
inspection，Studio 与客户端必须新建 thread。部署 v3 前，operator 必须按 graph ID 枚举 v2 的 pending/interrupt runs，
并逐个 drain 或 cancel；v1/v2 历史 checkpoint 只读，不得从 v3 resume/replay。项目控制的可运行 thread 在创建时
同时写入 SDK 原生 `graph_id` 和稳定 metadata `assistant_graph_id`。Agent Server auth 按 create 的 graph identity
把 Studio/项目 chat thread 规范为 v3，同时允许独立 Memory graph 使用自己的 identity；chat run create 与显式
metadata identity update 使用 owner + graph identity 过滤，因此旧 thread 不能靠 update 或 Studio 直连进入新图。
旧 run 的 interrupt/rollback 只按 owner 授权，仍可执行部署前的 v2 drain/cancel。`SdkAgentServerClient` 对返回的新建或
`if_exists="do_nothing"` existing thread 校验 `assistant_graph_id`，并在开始 `runs.stream` 前重新读取 thread
做同一精确校验。`assistant-native-v1`、`assistant-native-v2` 或缺失该字段的 unknown thread 在任何 v3 普通 run、resume 或 stream
开始前稳定拒绝，因此不会创建 run 或改变 checkpoint；thread/state/history 与既有 stream 的只读检查仍允许。
部署迁移所需的 v2 drain/cancel 也不受该 guard 阻止。guard 接受每次调用的 expected graph ID，不把 v3
硬编码成所有独立 Graph 的全局限制；Memory 等独立 Graph 在自己的运行边界使用自己的 graph ID。

`assistant-native-v3` graph 下保留系统创建的同名默认 assistant，并增加一个固定 planning preset assistant：

```text
assistant_id: 4cf38057-6071-50ca-a565-98b7854d763e
name: assistant-native-v3-planning
graph_id: assistant-native-v3
context.assistant_execution_mode: planning
```

它是同一 graph 的 Agent Server assistant 资源，不是第三张 graph，也不建立新的 Runtime 或 checkpoint schema。
Studio 选择该 assistant 后，messages-only input 在 `execution_router` 归一化为 planning；默认 assistant 仍按公开
input 的 `execution_mode` 路由并在省略时使用 fast。tokenless 本地 auth 只允许 create/update 上述固定 assistant ID，
并把 graph、name、context 与 metadata 强制规范为仓库定义；任意其他 assistant 写入和 delete 继续由默认 deny
拒绝。

新 assistant 与 run 必须选择 `assistant-native-v3`，Studio 用户也必须切换到该新 graph ID。媒体确定性
thread UUID 的 seed 包含 `assistant-native-v3`，因此同一 v3 connection 重连仍稳定，但不会命中旧 v1/v2 UUID；即便
命中一个外部指定的既有 ID，中央 metadata 校验仍然生效。CLI 的新 thread 与普通 `--thread-id` run 复用同一
guard。Agent Server 原生拥有 assistant、thread、run、
queue、checkpoint、interrupt/resume、cancel、stream 和 LangGraph Store。项目不再在生产入口维护第二份 run
manager、cancel token、checkpoint facade 或产品状态机。

公开 Graph 输入为严格 `AssistantRootInput`：

```json
{
  "messages": [{"role": "user", "content": "hello"}],
  "execution_mode": "fast"
}
```

`execution_mode` 只允许 `fast|planning|coding`，省略时默认为 `fast`；coding 还要求 `coding_repo_id`。
Memory Graph 的严格输入只有标准
messages；它由 Assistant Graph 通过 Agent Server SDK 调度，不向普通用户入口暴露 run type。
认证用户唯一来自 Agent Server 原生
`Runtime.server_info.user.identity`；`AssistantRunContext` 不复制用户或租户身份，只保存有默认值的
入口 profile、媒体能力，以及媒体入口在 chat 开始时签发的 opaque 视觉
capability token。每次 run 的公开
`execution_mode` 不放入 context；只有上述服务端持久 assistant 资源可通过窄
`assistant_execution_mode=planning` preset 覆盖 messages-only 默认值。窗口内容不进入标准 messages/context，
也不由模型或普通 Graph 输入提交。middleware 和 Tool 必须以
认证身份、thread 与 token 回到进程视觉模块解析冻结投影，伪造或过期 token 均 fail closed。

## 资源模型与 composition

| 资源 | 权威 owner | 含义 |
| --- | --- | --- |
| auth principal | Agent Server auth middleware | 调用方身份与 delegation 权限 |
| assistant/thread/run/checkpoint | Agent Server | Graph 定义、多轮状态与一次执行 |
| LangGraph Store | Agent Server | 可选跨 thread 数据资源，供 LangMem 等后端使用 |
| media connection | custom route | 一次 WebSocket 传输连接，不是 thread |
| delivery ID | custom route/outbox | 媒体 ACK 关联，不是 run 或 checkpoint |
| proactive delivery Store | custom route 与显式产品 publisher | 媒体连接 presence/claim/ACK；不是 LangGraph Store |
| Visual Perception Module | Agent Server 进程资源 | 视觉 authority 的进程级 owner，包含共享 embedding coordinator 与连接级视觉提醒 registry；不是 Graph Runtime |
| remote video archive | custom-app lifespan | 连接级 H.264 顺序归档、30 秒 MP4 切片、待上传 manifest 与临时下载 capability；不是 Graph Runtime |

Agent Server async factory 在每个 worker 进程首次取图时创建唯一 `AgentServerExecutionOwner`，持有标准
`BaseChatModel` Provider adapter、静态本地 `BaseTool`、一次发现得到的官方 MCP tools、一个
`MemoryBackend`、已编译但不绑定 checkpointer 的 `AssistantRootGraph` 与
`AssistantMemoryExtractionGraph`；后续两个 graph 的 schema、history、state 与 run 取图全部复用同一 owner，
不重复装配。LangMem 引用首次 factory 注入的进程 Store；custom-app
lifespan 在进程 shutdown 时统一关闭 owner。进程级 `VisualPerceptionModule` 的内部算法和资源边界由视觉
authority 定义；run-local Tool 只借用其窄消费接口，不创建第二套视觉流水线。
custom-app lifespan 启动后通过同进程本机 Agent Server 的只读 graph endpoint 触发一次总时长有界的预热，使
Provider、MCP discovery 与静态 graph composition 在媒体 WebSocket 握手完成前结束；预热失败只记录安全告警，
后续正式 graph 访问仍可按原生 factory 路径完成装配。
`http.app` 的 FastAPI lifespan 是该模块的进程 owner：API Server、queue worker 或独立 custom app 各自在
本进程 shutdown 时关闭一次。graph factory、schema/history/state 请求和单个 run 只借用该模块，不参与
关闭；媒体 WebSocket 只关闭自己创建的 `VisualPerceptionSession`。

composition 在构造 Tool inventory 前只加载一次 repo Skill catalog，并把同一实例显式注入 inventory 的
Skill loading plugin 与 fast agent；planning coordinator 通过 Deep Agents `CompiledSubAgent` 直接引用该 fast agent，
不再次读取 catalog。composition 构造标准模型、该 Tool inventory、Memory backend 与两张静态原生 Graph，不构造
平行 Graph Runtime、产品状态投影器或 Workflow host。

## 本地部署与持久化

`scripts/run_server.py` 提供两个显式 backend：

- `dev` 使用 `langgraph dev`，checkpoint 与 Store pickle 到仓库 `.langgraph_api/`。该目录由整个工作目录共享，
  只允许同时运行一个 dev server；多个端口并行运行会竞争同一份退出落盘状态，不作为可靠持久化方案。
- `postgres` 使用 `langgraph build` 生成 `assistant-agent/langgraph-api:local`，再由
`deploy/agent_server/compose.yaml` 启动 Agent Server、PostgreSQL 16/pgvector 与 Redis 6。Agent Server
通过 `POSTGRES_URI` 持久化 assistant、thread、run、checkpoint 与 LangGraph Store，Redis 只承担运行时
stream/queue 协调。LangMem 仍使用 factory 注入的 Agent Server Store，不引入项目自有 PostgreSQL adapter。
`langgraph.json` 同时声明运行镜像所需的 LangMem optional runtime 依赖，不能只依赖宿主 Python 环境已安装的
extra。

当前 dev backend 使用基于官方 `langgraph-runtime-inmem==0.32.4` 的最小本地 fork
`0.32.4+assistant1`，补丁与可重复构建入口分别是
`patches/langgraph-runtime-inmem/0.32.4-event-wakeup.patch` 和
`scripts/install_patched_inmem_runtime.py`。该 fork 仍由 Agent Server runtime 原生拥有 queue：创建即时 pending
run，或 worker 完成并释放 thread（包括 retry run 已恢复为 pending）后，通过跨 event loop 的 generation
signal 唤醒唯一 queue scheduler；scheduler 先扫描再等待，避免 lost wake-up。`after_seconds` delayed run 按最早
`created_at` 设置 deadline，空闲时只保留 5 秒安全 heartbeat，不再以 500 ms 固定扫描作为即时 chat run 的
启动路径。补丁只属于 `langgraph dev`；postgres/Redis backend 不加载它。安装器固定校验官方 wheel SHA-256，
上游内容或接口漂移时必须先人工 rebase，禁止静默套用。

本地 dev 的唯一常驻入口由 PyCharm 管理，固定使用 `8089` 并保留 `langgraph dev` 原生 hot reload。Codex 默认
作为客户端连接该服务；修改源码后先等待 reload，只有需要完整重启时才重启同一个 `8089` 实例。dev backend
若临时使用 `8090`，仅供 PyCharm Server 已停止后的隔离诊断，诊断完成即停止；postgres backend 则把 `8088`
作为独立持久化部署的默认端口。`scripts/run_server.py` 对 dev backend 持有工作目录级
单实例锁，并在启动前要求请求端口可用，禁止框架自动漂移到随机端口；默认日志按请求端口写入
系统临时目录下的 `assistant_agent/logs/agent_server-<port>.log`，避免日志写入触发源码 watcher 后形成
自反馈 reload；dev 显式日志路径同样不得位于仓库监听树内。postgres 日志仍写入
`.data/logs/agent_server-<port>.log`。dev 启动时 wrapper 会从受版本控制的 `langgraph.json` 生成一次性配置，
只把 `env` 字段替换为 `--env-file` 指定的绝对路径；`--no-env-file` 则替换为空对象并只继承进程环境，避免
LangGraph CLI 再从仓库 `.env` 覆盖显式 mock 配置。一次性配置位于系统临时目录，进程退出后删除。
这些约束不创建项目自有 Runtime，也不把两个端口解释为两个 worker。

postgres backend 的 API 仅映射到 `127.0.0.1:${ASSISTANT_AGENT_SERVER_PORT}:8000`，默认宿主端口为 8088；
PostgreSQL 与 Redis 不映射宿主端口，也不复用旧 Langfuse 服务。PostgreSQL 数据保存在独立 named volume
`assistant-agent-langgraph-postgres-data`，普通 `restart`、`stop` 或重新构建 API 镜像不会删除数据。
`.langgraph_api/` 不迁移到 PostgreSQL。`.env` 仅作为未跟踪的容器 env file 注入，不能写入镜像或提交。

首次构建启动：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --backend postgres --host 127.0.0.1 --port 8088 --env-file .env --rebuild
```

代码未变化时省略 `--rebuild`。此入口以前台 Compose 进程运行，Ctrl-C 停止该专用 stack，但保留 PostgreSQL
volume。删除 volume 属于显式数据销毁操作，不是正常停止流程。

operator 需要检查或恢复 conversation 时，`scripts/agent_cli.py` 直接使用公开 SDK：
`threads.get_history()` 只显示 checkpoint 元数据，`runs.wait(input=None, checkpoint_id=...)` 从历史
checkpoint 创建 replay 分支，`runs.cancel(action="rollback", wait=True)` 丢弃仍可取消的 run 及其
checkpoints。replay 与 rollback 都要求精确确认；项目不读取 saver、不维护 checkpoint facade，也不把 Graph
state 回滚描述为已完成外部 Tool 副作用的自动撤销。

所有当前 chat 入口最终调用同一个 `assistant-native-v3`，因此 Memory debounce 不散落在 Studio、CLI、HTTP 或
WebSocket adapter：主图在回答后使用官方 SDK 查找并 rollback 同 thread、带专用 metadata 的旧 pending Memory
run，随后 enqueue 一个新的 delayed `assistant-memory-v1` run。Agent Server 继续拥有真正的 delay 与
queue；项目不创建 timer 或第二套队列。

## Auth 与身份

LangSmith Studio 携带 Agent Server 内建的 `x-auth-scheme: langsmith` 认证并构造 `StudioUser`；非 Studio
客户端在 mock 与 real 模式下都通过 tokenless auth hook 构造 developer principal：`X-Assistant-User` 存在时
直接作为 identity，省略时使用 `local-developer`。项目不读取 Bearer token，也不校验 delegation 签名。
thread metadata 仍按 auth owner 限制，run 与 Store 沿用同一 principal；但 identity 由客户端声明，因此该部署
不具备跨不受信网络的身份认证能力，不应把端口暴露给不受信调用方。connection、vendor session、thread、run
与 delivery ID 始终是不同身份轴。

## `/agent-service/v1`

custom route 只负责：

- 解析、校验 vendor frame；
- 关联 connection、vendor session、native thread/run、chat 与 delivery；
- 使用公开 `langgraph_sdk` 创建 run、消费 resumable stream、join 与 cancel；
- 从 terminal values 选择最新标准 `AIMessage` 并机械投影媒体响应；
- 把解码帧提交给连接级视觉句柄，并把视觉模块返回的可信目标边界投影到 chat；
- 按 native thread 从主动投递 Store 串行 claim，处理 ACK、lease 与重连补投；
- 承载不执行 Graph 的 callback route。

同一 custom app 还提供只读 `/artifacts/generated/{filename}`，供受信程序消费者读取图像 Tool 落盘后的
受管图片。该路由只接受受管目录中的单层文件名，并限制文件大小和可识别图片 MIME；配置
`ARTIFACT_BASE_URL` 时，图像 Tool 会在 `ToolMessage.artifact.images[].url` 中附带客户端可访问的绝对 URL。
当前 Studio 不保证渲染 Tool artifact。

显式启用远端视觉记忆时，同一 custom app 还提供
`/internal/memory-media/{opaque-token}`。该路由只解析进程内、带 TTL 的一次任务 capability，返回已完成的
受管 MP4；不接受任意路径，也不承担视频处理或 Memory 调度。归档服务及 SQLite 待上传 manifest 由
custom-app lifespan 创建、恢复和关闭。

它不读取 checkpoint，不执行 Tool/Memory，不构造旧 Runtime，也不翻译项目 run/error 状态机。媒体 SDK stream
只订阅 messages/values：messages 投影模型正文增量，values 读取权威终态；updates/custom 仅留给显式选择这些
模式的 Agent Server 原生 SDK 或 Studio 消费者。短暂订阅断开后按 last event ID 调用
`threads.join_stream`。WebSocket 断开时
best-effort cancel 当前连接仍活动的 reactive runs；delivery ACK 不改变 run 或 checkpoint。

主动投递不进入 `AssistantRootGraph`；当前图没有业务生产者，因此不保留休眠的 state channel 或 dispatch
节点。显式产品 publisher 可按稳定 message ID 写入独立 Store。媒体连接启动 thread-specific pull pump；
durable 行只有匹配 ACK 才完成，断线或超时释放为
queued，ephemeral 离线时直接 skipped。当前 SQLite 实现面向单实例或共享受控卷，不宣称多主机一致性。

H.264 解码与 3D callback 属于媒体边缘资源。解码后的 JPEG 保存在连接级有界临时目录，最近帧引用只进入
进程级有界内存 frame index，断线时索引与临时 JPEG 一并清理；Graph State 只携带稳定引用，不为实时帧建立
SQLite 热路径。解码帧提交后的并发观察、关键帧、文本发布和目标帧等待全部由视觉 authority 负责；Agent
Server 只传递稳定引用与可信目标边界。3D callback 只向当前在线连接发布中性 artifact，不启动第二次 Graph。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/contract/test_gateway_contract.py \
  tests/core/integration/test_runtime_lifecycle.py
```

### Stage5B snapshot bounded-enumeration addendum

Coding analysis 的 baseline index 不得通过 `subprocess.run(capture_output=True)` 物化完整
`git ls-files -z` 输出。实现使用受治理的 `Popen` stdout 分块读取和跨 chunk NUL record
解析，在读取及 record 边界即时消耗 scan bytes/entries；达到硬限后立即终止并回收 Git
子进程，stderr 只保留固定上限。源仓库 object format 必须通过禁 lazy fetch、禁 credential
prompt、隔离 system/global config 的 `git rev-parse --show-object-format` 获得，并且只接受
`sha1` 或 `sha256`；canonical bare metadata 根据该枚举生成全新 config，不复制源仓库 config。

周期 reaper 只保留常数数量的 root traversal 与单一 active snapshot deletion cursor；每个 round 对当前
management root 只读取固定 snapshot child slice 后立即推进下一个 root，各 workspace 的固定大小 directory
cookie 保存 snapshot 枚举进度，从而对任意数量 workspace 保持 eventual progress。过期 snapshot 目录先在其
`analysis-snapshots` 父目录内 atomic rename 为固定前缀 tombstone，再由跨轮单一增量 DFS cursor 按共享
entry/time budget 执行 `scandir`、`unlink` 与 `rmdir`；不得把任意大小 snapshot 目录作为一次 `rmtree`
操作。DFS 每轮从受管 `analysis-snapshots` parent dirfd 重新开始，只以 relative component、directory cookie
和 expected device/inode 保存有界跨轮状态，不跨轮持有 fd、Path iterator 或 `scandir` handle；每层必须通过
`openat(O_DIRECTORY|O_NOFOLLOW)` 与 `fstat` 复核，所有 `stat`、`unlink`、`rmdir` 都使用 parent dirfd-relative
操作，并在 mutation 前重新校验完整 ancestor/target inode chain。root 或 nested replacement 必须 fail closed，
不得跟随 symlink 或操作受管 parent 之外的对象。workspace root 消失、进程关闭或 traversal 重置时必须关闭并
清除所有 descendant cursor。

### Stage5B reaper fairness and process-deadline addendum

所有 coding snapshot Git `Popen` 的 stdout/stderr pipe 必须设为 nonblocking，并由 selector
以 absolute deadline 的剩余时间等待；实际读取只使用事件就绪后的 `os.read`。deadline 到达、
输出预算超限或 parser/budget 失败时，owner 必须 terminate、bounded wait、必要时 kill/wait，
随后关闭 selector 与全部 pipe。不得在 deadline 检查后调用可能阻塞的 file-object `read`。

周期 reaper 对 workspace root 使用 round-robin traversal；每个 cleanup round 对单个受管 management root
只消费固定 snapshot child slice，然后推进下一个 root。snapshot Linux directory cookie 通过固定大小、atomic
replace 的受管 progress metadata 跨轮保存，避免关闭 cursor 后从大目录开头重扫；内存只保留 root traversal、
当前 page 和至多一个 active snapshot tombstone DFS。root 消失时必须同时清理 hierarchical traversal 与兼容
cursor 的全部 descendant iterator。progress metadata 只能由 management-root dirfd 通过
`O_RDONLY|O_NONBLOCK|O_NOFOLLOW` 打开，且必须是当前 uid 所有、大小不超过固定上限的 ordinary file；读取循环
受 absolute deadline 与 byte limit 约束。FIFO、symlink、directory、oversize、I/O error 或 schema 错误一律安全
回到 cookie `0`，不得阻塞或传播卡死；JSON 只接受字段精确、`schema_version` 为整数 `1` 的 object，`null`、
list、scalar、missing/wrong schema、bool-as-int 或字段越界均无效。写入继续使用 no-follow temporary file、`fsync`
与同 parent dirfd atomic replace。periodic cleanup 与 `aclose` 通过 process-owned mutex 串行。

snapshot tombstone DFS 一旦发现 root/nested inode replacement，必须立即关闭并释放全局 deletion slot，不删除
可疑对象。owner 以常数上限 LRU cooldown 和 tombstone 同目录、ordinary/no-follow/atomic 写入的 poison marker
避免每轮重新抢占同一可疑目录；marker reader 同样只接受字段精确、`schema_version` 为整数 `1` 且字段类型/范围
有效的 JSON object，所有其他形状或 contract 异常均视为 marker 不存在且不得传播。其他 snapshot 仍须获得
eventual cleanup。workspace root 消失时同步清空进程内 deny/cooldown state。

本 Stage 不改变 Git worktree retirement 协议。周期 owner 和 `aclose` 不得调用 `cleanup_expired()`、
`git worktree remove` 或任何 Git common-dir/admin registry 清理，也不得 tombstone 或递归删除 management root；
workspace 到期仍由后续 `resolve()` 进入既有同步 cleanup 路径处理。该边界避免 advisory snapshot TTL cleanup
引入第二套 workspace/admin 事务。

### Stage 5C final review 字节绑定（2026-08-24）

- Final review 的启用开关是受信、repository-static 的 `CodingRepositoryConfig.code_review_enabled`，默认关闭；`AssistantRunContext` 不暴露用户可控开关。integration 仍由独立、默认关闭的 `integration_enabled` 控制。
- `run_validation` 在执行命令前创建 immutable-manifest v2 snapshot，命令只针对该 snapshot materialization；验证后必须重新得到同一 `snapshot_ref`、`tree_digest` 与 `workspace_diff_digest`，并生成包含 cycle generation、snapshot identity 和全部 command evidence 的 `validation_binding_digest`。
- `prepare_review_snapshot` 不重新抓取 live workspace，而是复用 validation 产出的 snapshot。review input/result/report 必须回显 generation、snapshot ref、tree digest、workspace diff digest、validation evidence digest 和固定 task inventory。
- current v2 checkpoint 的 snapshot/path/digest/permission/identity/expiry/manifest 错误全部 fail closed 为 `coding_review_binding_mismatch`，不得投影为可批准的 unavailable。仅 completed `legacy_v1` 保留旧物理 snapshot 已回收时的兼容恢复；pending downgrade 继续 fail closed。
- 审批后的 commit 接收同一 validation snapshot 与 canonical review report digest；临时 Git index 生成的 tree object 经 `sha256(tree_oid)` 必须等于 reviewed `tree_digest`，否则拒绝提交。commit trailer 和 `CodingCommitResult` 同时记录 validation/report/tree binding。
- validation failure/workspace-change 不交接 snapshot lease；terminal summarize 幂等释放 validation/review snapshot；commit comparison 无论成功或失败都释放 expected/current snapshot。review approve 且 integration 开启时，父 checkpoint 在 commit 消费前继续持有 active lease；这些 release 不新增 periodic reaper，也不触碰 worktree 或 Git admin lifecycle。
