# Assistant Memory Plugin API 设计

日期：2026-08-07

状态：已批准

## 1. 背景

当前 `assistant_agent` 的长期记忆由 `LongTermMemoryService` 固定编排，并直接依赖 Mem0 client、session snapshot store 和后台 ingestion queue。当前权威文档明确限定只支持 Mem0，因此虽然 Mem0 client 已经承担远端服务 adapter 职责，Runtime 还没有面向第三方记忆实现的稳定扩展契约。

本设计引入原生 Python `Assistant Memory Plugin API`。Mem0 不再是 Runtime 内建的特殊分支，而是默认内置的 `Mem0MemoryPlugin`；现有 `Mem0Client` 保留为该 Plugin 私有的底层 HTTP/service adapter。Runtime 不再了解 Mem0 API、配置字段或响应格式，只通过统一 Memory Plugin Host 与当前唯一活动的 Memory Plugin 通信。

目标结构为：

```text
Assistant Runtime
  -> MemoryPluginHost
  -> Assistant Memory Plugin API
  -> active MemoryPlugin
       -> Mem0MemoryPlugin
            -> Mem0Client
            -> Mem0 service

       or

       -> ThirdPartyMultimodalMemoryPlugin
            -> third-party client
            -> third-party memory service
```

## 2. 目标

1. 定义强类型、版本化的 `assistant_memory_plugin_v1` 宿主契约。
2. 同一 Runtime 只允许一个排他的主 Memory Plugin，通过 `memory` slot 选择。
3. Plugin 掌管记忆召回、排序、提取、更新、合并、多模态处理和远端服务交互。
4. Runtime 保留身份绑定、授权、媒体访问、上下文预算、安全投影、后台副作用调度和审计等不可绕过的治理边界。
5. Plugin 只返回结构化记忆贡献，不能直接读取或修改 `AgentState`、Prompt 或 Provider request。
6. 第一版支持受信任的进程内 Python Plugin；外部 Plugin 由 operator 显式配置，配置变化重启生效。
7. 将现有 Mem0 行为迁移为默认内置 Plugin，并保持默认产品行为、mock/offline 安全和现有观测语义。
8. 为未来的文本、图片、音频、视频和文档记忆服务提供受管媒体引用边界。

## 3. 非目标

- 不直接运行或兼容 OpenClaw 的 TypeScript Plugin。
- 不实现通用 Runtime 事件总线；Memory 的关键控制流使用专用强类型方法。
- 不支持多个主 Memory Plugin 同时召回、写入或合并 context。
- 不把 Memory 改造成默认模型可调用 Tool，也不新增 `memory_search`、`memory_get` 或 `memory_save` Tool。
- 不允许 Plugin 直接拼接 system/developer/user prompt，也不允许修改 `ChatRequest.messages`。
- 不在第一版提供 Plugin marketplace、install、update 或 uninstall。
- 不把进程内 Python Plugin 描述为不可信代码沙箱。
- 不在 v1 定义跨进程 Plugin RPC 协议。
- 不在 `close_session()` 中隐式执行大规模 consolidation；如果未来需要 consolidation，应设计独立、显式、可调度的能力。

## 4. 核心原则

### 4.1 固定生命周期方法优先

Runtime 直接调用 Memory Plugin 的固定方法，而不是广播可被任意监听器修改的通用事件：

```python
class MemoryPlugin(Protocol):
    descriptor: MemoryPluginDescriptor

    def open_session(
        self,
        request: MemorySessionOpenRequest,
    ) -> MemorySessionOpenResult: ...

    def prepare_context(
        self,
        request: MemoryContextRequest,
    ) -> MemoryContextContribution: ...

    def ingest_turn(
        self,
        request: MemoryTurnIngestionRequest,
    ) -> MemoryTurnIngestionResult: ...

    def close_session(
        self,
        request: MemorySessionCloseRequest,
    ) -> MemorySessionCloseResult: ...
```

这些方法构成 v1 的完整关键控制流。旁路观测可以继续使用现有 observer/trace 机制，但观测事件不能替代上述方法、修改其返回值或获得 Memory 权限。

### 4.2 Plugin 代码可信，记忆内容不可信

进程内 Plugin 属于 Runtime 的可信代码基；安装它等价于允许 Python 代码在 Assistant 进程中运行。Host API 可以约束正常实现的交互契约，但不能阻止恶意 Python 代码主动读取环境、文件或网络。

Plugin 返回的 memory text、metadata 和媒体证据仍是不可信历史数据。它们不能成为 system/developer instruction，也不能覆盖当前用户请求、Runtime policy、ToolSpec 或授权结果。

### 4.3 Host 掌管治理，Plugin 掌管算法

Plugin 负责：

- 召回、搜索、排序和相关性算法；
- 事实提取、更新、合并、删除和持久化算法；
- 文本与多模态记忆的关联；
- embedding、VLM 或第三方服务调用；
- Plugin 私有 session、缓存和远端 conversation 状态；
- 选择建议注入的结构化记忆。

Runtime/Host 负责：

- 可信身份和 Plugin-scoped identity；
- slot 选择、API version 和配置校验；
- timeout、取消、并发、有界队列和 shutdown；
- 受管媒体读取与 artifact 登记；
- 返回值 schema、大小和安全校验；
- context budget、去重、裁剪和 prompt 编译；
- 审计、脱敏和运行期降级；
- 外部写入的幂等键和调度顺序。

## 5. 组件与所有权

### 5.1 `MemoryPlugin`

第三方实现的运行时能力接口，只接收不可变的 Pydantic 请求并返回结构化 Pydantic 结果。Plugin 不接收可变 `AgentState`、PromptCompiler、ToolRegistry、Auth provider、TraceStore、EventSink 或其他 Plugin 实例。

v1 方法为同步 Python 方法。Host 负责把可能阻塞的调用放入受控 worker，并负责后台写入调度；v1 不再定义一套重复的 async Plugin API。

### 5.2 `MemoryPluginHost`

Runtime 面向的唯一 Memory 宿主边界：

```python
class MemoryPluginHost:
    def open_session(...) -> MemorySessionOpenResult: ...
    def prepare_context(...) -> MemoryContextContribution: ...
    def schedule_ingestion(...) -> bool: ...
    def close_session(...) -> MemorySessionCloseResult: ...
```

Runtime 不直接调用 Plugin。Host 保存 `memory_session_id`、Plugin 私有 `session_handle`、session baseline、本轮冻结 contribution、健康状态和后台任务元数据。

### 5.3 `MemoryPluginRegistry`

Registry 在启动期发现显式内置和显式配置的 Plugin factory，完成全量校验后选择唯一 `memory` slot 并 seal。Registry 负责 inventory 和所有权；Host 负责调用当前活动 Plugin。

以下情况必须在启动期 fail closed：

- `plugin_id` 重复；
- descriptor、kind 或 API version 不合法；
- 配置 slot 指向未知、禁用或未成功构造的 Plugin；
- 同时声明多个活动主 Memory Plugin；
- module export、factory、config schema 或 Plugin 构造失败。

### 5.4 `MemoryPluginFactory`

外部 module 导出：

```python
__assistant_memory_plugin_factory__
```

Factory 协议为：

```python
class MemoryPluginFactory(Protocol):
    descriptor: MemoryPluginDescriptor
    config_model: type[BaseModel]

    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> MemoryPlugin: ...
```

使用 factory 而不是 import 时构造的 singleton，使 Host 可以先校验 descriptor 和配置，再注入受控依赖；module import 不应连接远端服务。

### 5.5 Mem0 所有权

最终所有权必须为：

```text
MemoryPluginHost
  -> Mem0MemoryPlugin
       -> Mem0Client
```

`Mem0MemoryPlugin` 负责 Assistant Memory Plugin API 与 Mem0 domain/client API 的转换；`Mem0Client` 只负责 Mem0 URL、鉴权、HTTP、分页、timeout、原生请求响应和 Mem0 特有错误。

完成迁移后，Runtime 层不得出现 `runtime.mem0_client`、Mem0 HTTP 字段或根据 `provider == "mem0"` 分支。只有 `Mem0MemoryPlugin` 及其私有 adapter 可以了解 Mem0 特有语义。

## 6. Descriptor 与能力声明

```python
class MemoryPluginDescriptor(BaseModel):
    plugin_id: str
    plugin_version: str
    api_version: Literal["assistant_memory_plugin_v1"]
    kind: Literal["memory"] = "memory"
    capabilities: MemoryPluginCapabilities
```

```python
class MemoryPluginCapabilities(BaseModel):
    modalities: set[
        Literal["text", "image", "audio", "video", "document"]
    ]
    supports_session_recall: bool
    supports_turn_ingestion: bool
    supports_context_refresh: bool
    supports_idempotent_ingestion: bool
```

能力声明用于 Host 校验和调用决策，不自动授予媒体、身份、网络、context 或删除权限。v1 不声明 Host 无法调用的 deletion/consolidation capability；Plugin 可以在一次已授权的 `ingest_turn()` 内按自身算法产生 `deleted` change，但 Runtime 主动删除或 consolidation 需要未来新增独立、显式的方法和治理契约。

## 7. 标准数据契约

Request model 使用 `ConfigDict(arbitrary_types_allowed=True)` 承载下列不序列化的进程内取消句柄：

```python
class MemoryCancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...
```

### 7.1 Plugin-scoped identity

```python
class MemoryIdentity(BaseModel):
    user_id: str
    agent_id: str
    session_id: str
    tenant_id: str | None = None
    project_id: str | None = None
```

这些字段全部由 Host 从可信 `AuthContext` 和 `RequestIdentity` 构造。Host 使用 Plugin-specific namespace 将 Runtime identity 稳定映射为不透明 ID，用户 metadata 不能提供或覆盖这些值；不同 Plugin 不能共享未经显式迁移的 identity namespace。

### 7.2 Session 打开

```python
class MemorySessionOpenRequest(BaseModel):
    memory_session_id: str
    identity: MemoryIdentity
    opened_at: datetime
    entry_profile: str
    deadline: datetime
    cancellation: MemoryCancellationToken
```

```python
class MemorySessionOpenResult(BaseModel):
    status: Literal["ready", "degraded", "unavailable"]
    session_handle: str | None
    initial_contribution: MemoryContextContribution | None
    issues: list[MemoryPluginIssue]
```

`session_handle` 是 Plugin 私有的不透明值，只保存在 Host，只回传给创建它的同一 Plugin，不进入 prompt、日志、API 或公开 trace。切换 Plugin 后不能复用旧 handle。

### 7.3 Context 请求和贡献

```python
class MemoryContextRequest(BaseModel):
    memory_session_id: str
    session_handle: str | None
    identity: MemoryIdentity
    current_turn: MemoryMessage
    media_refs: list[ManagedMediaRef]
    context_budget_hint: MemoryBudgetHint
    deadline: datetime
    cancellation: MemoryCancellationToken
```

```python
class MemoryContextContribution(BaseModel):
    items: list[MemoryContextItem]
    status: Literal["succeeded", "partial", "unavailable"]
    issues: list[MemoryPluginIssue]
```

```python
class MemoryContextItem(BaseModel):
    memory_id: str
    text: str
    source: Literal[
        "long_term",
        "episodic",
        "semantic",
        "visual",
        "audio",
        "document",
    ]
    relevance: float | None
    occurred_at: datetime | None
    created_at: datetime | None
    media_refs: list[ManagedMediaRef]
    metadata: dict[str, JsonValue]
```

Plugin 不能返回 role message、prompt patch、system/developer instruction、绝对路径、凭据或 inline Base64 media。`context_budget_hint` 是预算提示而不是授权；Host 仍会独立执行硬限制。

### 7.4 Turn 写入

```python
class CompletedMemoryTurn(BaseModel):
    user_message: MemoryMessage
    assistant_message: MemoryMessage
    tool_evidence: list[MemoryToolEvidence]
    media_refs: list[ManagedMediaRef]
    occurred_at: datetime
```

```python
class MemoryTurnIngestionRequest(BaseModel):
    memory_session_id: str
    session_handle: str | None
    identity: MemoryIdentity
    turn: CompletedMemoryTurn
    idempotency_key: str
    deadline: datetime
    cancellation: MemoryCancellationToken
```

```python
class MemoryTurnIngestionResult(BaseModel):
    status: Literal["accepted", "partial", "rejected", "failed"]
    changes: list[MemoryChange]
    issues: list[MemoryPluginIssue]
```

```python
class MemoryChange(BaseModel):
    operation: Literal["created", "updated", "deleted", "unchanged"]
    memory_id: str
    memory_type: str | None
```

普通日志和公开 trace 只记录 ID、数量与 operation，不记录记忆正文、原始消息和远端响应。

### 7.5 Session 关闭

```python
class MemorySessionCloseRequest(BaseModel):
    memory_session_id: str
    session_handle: str | None
    identity: MemoryIdentity
    reason: Literal[
        "normal", "reset", "expired", "shutdown", "plugin_replaced"
    ]
    deadline: datetime
    cancellation: MemoryCancellationToken
```

```python
class MemorySessionCloseResult(BaseModel):
    status: Literal["closed", "partial", "failed"]
    issues: list[MemoryPluginIssue]
```

`close_session()` 必须幂等，只释放 Plugin 私有 session 资源；它不隐式获得额外写入或 consolidation 权限。

所有 request model 允许携带一个不序列化、不进入日志和 Plugin 配置的进程内 `MemoryCancellationToken`。Plugin 应在远端调用、媒体读取和批处理边界协作检查它；`deadline` 是绝对时限，token 是提前取消信号。Host 到期或取消后丢弃迟到结果，但不声称能够强杀 Plugin 已启动的 Python 线程或网络工作。

### 7.6 错误契约

```python
class MemoryPluginIssue(BaseModel):
    code: str
    message: str
    recoverable: bool
    retry_after_seconds: float | None
```

Plugin 抛出的异常不能穿透 Runtime。Host 将其转换为稳定、脱敏的宿主错误：

```text
memory_plugin_timeout
memory_plugin_unavailable
memory_plugin_invalid_result
memory_plugin_internal_error
```

## 8. 生命周期和数据流

### 8.1 Session 创建

```text
runtime session.create
  -> Host 生成 memory_session_id
  -> Host 构造 plugin-scoped identity
  -> Plugin.open_session()
  -> Host 保存 session_handle
  -> Host 校验并冻结 initial contribution 为 session baseline
```

`open_session()` 每个 Runtime session 只调用一次；重复初始化由 Host 去重。

### 8.2 每个 user turn 的召回

```text
user request
  -> Runtime 收集当前文本和 ManagedMediaRef
  -> Host.prepare_context()
  -> Plugin 返回本轮完整 contribution
  -> Host 与 session baseline 按 memory_id 去重
  -> 本轮结果覆盖同 ID baseline
  -> ContextBuilder 执行安全投影和预算裁剪
  -> PromptCompiler 编译
```

`prepare_context()` 每个 user turn 最多调用一次，结果在本次 Agent run 中冻结。同一 ReAct run 内的多次 LLM 和 Tool 调用复用同一 contribution，避免召回漂移、重复费用和 prompt cache 抖动。

`supports_context_refresh=false` 时，Host 不调用 `prepare_context()`，每个 turn 只使用 session baseline。Plugin 返回当前 turn 的完整候选集合，不返回增删 patch。

### 8.3 Turn 写入

```text
Runtime 形成 final response
  -> 记录 response.delivered
  -> Host 构造 CompletedMemoryTurn
  -> Host 将任务加入后台有界队列
  -> Plugin.ingest_turn()
```

同一 `user_id + agent_id + session_id` 串行，不同身份可以并行。Host 生成稳定 idempotency key；队列满、timeout 或 Plugin 失败只记录结构化失败，不把已经完成的 Agent run 改为失败。

Plugin 决定是否提取记忆、如何更新和关联多模态内容。Host 仍可基于结构化 Runtime 事实跳过不应进入长期记忆的 turn；该治理不能由请求文本、关键词或 Plugin 自行绕过。

### 8.4 Session 关闭

```text
session reset / expiry / shutdown
  -> Host 停止接受新的该 session ingestion
  -> 有界等待已接收任务
  -> Plugin.close_session()
  -> Host 清除 handle、baseline 和冻结 contribution
```

## 9. 多模态边界

Runtime 只传递受管引用：

```python
class ManagedMediaRef(BaseModel):
    ref_id: str
    media_type: Literal["image", "audio", "video", "document"]
    mime_type: str
    size_bytes: int
    created_at: datetime
    owner_scope: str
```

Plugin 通过构造期注入的 Host capability 读取内容：

```python
class MemoryMediaReader(Protocol):
    def read(
        self,
        ref: ManagedMediaRef,
        *,
        max_bytes: int,
    ) -> bytes: ...

    def open_stream(
        self,
        ref: ManagedMediaRef,
        *,
        max_bytes: int,
    ) -> BinaryIO: ...
```

Reader 校验 owner/session、Plugin modality、单文件与单 turn 大小、引用有效期、取消和超时，并拒绝目录、符号链接及任意路径。Plugin 可以在自己的 adapter 内把已授权内容上传到远端服务，但必须自行设置 HTTP timeout、清理临时缓冲并避免记录凭据或原始媒体。

Plugin 如需产生新媒体证据，只能通过受管 artifact writer 登记：

```python
class MemoryArtifactWriter(Protocol):
    def register(
        self,
        payload: MemoryArtifactPayload,
    ) -> ManagedMediaRef: ...
```

Plugin 不能向模型返回本地绝对路径、`file://` URI、未治理下载 URL 或 inline Base64。

## 10. Build Context 和配置

```python
@dataclass(frozen=True)
class MemoryPluginBuildContext:
    provider_mode: Literal["mock", "real"]
    media_reader: MemoryMediaReader
    artifact_writer: MemoryArtifactWriter
    secret_resolver: MemorySecretResolver
    clock: Clock
```

建议的本机配置文件为：

```json
{
  "schema_version": "assistant_memory_plugins_v1",
  "slot": "mem0",
  "plugins": {
    "mem0": {
      "enabled": true,
      "module": "assistant_agent.memory.plugins.builtin.mem0",
      "config": {
        "base_url": "${MEM0_BASE_URL}",
        "api_key": "${MEM0_API_KEY}"
      }
    }
  }
}
```

入口环境变量为：

```text
MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH
```

配置文件不保存真实 secret，只允许 Host 支持的 SecretRef/环境引用形式。descriptor、装配报告和错误不得回显解析后的 secret。

为保持兼容，如果没有提供新配置文件，composition root 按现有环境装配默认内置 `mem0` slot；mock mode 和 Mem0 未完整配置时，该 Plugin 明确报告 `unavailable` 且不联网，不静默切换到另一个真实 Plugin。显式配置了未知或无效 slot 时必须启动失败，不能退回 Mem0。

不执行目录扫描、Python entry point 自动启用或“检测到 API key 即启用”。

## 11. 执行策略

Host 配置独立的调用和资源上限：

```python
class MemoryPluginExecutionPolicy(BaseModel):
    open_session_timeout_seconds: float
    prepare_context_timeout_seconds: float
    ingest_turn_timeout_seconds: float
    close_session_timeout_seconds: float
    max_context_items: int
    max_context_chars: int
    max_media_items_per_turn: int
    max_media_bytes_per_turn: int
```

- `open_session` 和只读 `prepare_context` 最多重试一次可恢复失败。
- `ingest_turn` 只有在 Plugin 声明 `supports_idempotent_ingestion=true` 时才允许 Host 自动重试。
- `close_session` 要求 Plugin 自身幂等，shutdown 时仅 best effort。
- timeout 后 Host 丢弃迟到结果；连续失败可以将活动 Plugin 标记为当前进程 `unhealthy`。
- 进程内 Python 无法安全强杀卡死线程，因此 Host 不声称 timeout 已终止 Plugin 自有网络、线程或后台工作；Plugin 必须为其 I/O 设置独立 timeout。

## 12. 返回值、安全与观测

Host 在接受结果前校验：

- item 数量、总字符和 metadata JSON 大小；
- memory ID、session handle 和 issue 字段格式；
- relevance 范围和时间字段；
- source 与 Plugin capability；
- media ref 是否由当前 Host 签发且属于当前 owner；
- 是否包含绝对路径、凭据、inline media、role message 或 prompt patch。

无效 Plugin 结果整体拒绝为 `memory_plugin_invalid_result`，不把部分解析成功内容注入 context。

运行期降级规则：

| 生命周期 | 失败行为 |
| --- | --- |
| `open_session` | 建立 degraded Memory session，baseline 为空 |
| `prepare_context` | 本轮贡献为空，继续回答 |
| `ingest_turn` | 记录失败，不改变已完成的 Agent run |
| `close_session` | best effort 清理并记录风险 |
| 媒体读取 | 拒绝该媒体项，保留其他安全项目 |

每次调用只记录 prompt-safe 元数据：

```text
plugin_id
plugin_version
api_version
memory_session_id
operation
status
latency_ms
item_count
media_count
change_counts
issue_codes
retry_count
timeout
```

默认不记录 memory text、原始 user/assistant message、媒体正文、session handle、API key、Plugin 原始异常和远端原始响应。正文观测只能复用现有本地、显式开启的受限 trace content 机制。

## 13. CLI 与诊断

新增只读命令：

```bash
python -m assistant_agent.memory.cli plugins
```

输出活动 slot、Plugin identity、source、enablement、readiness、seal generation 和脱敏 issues。该命令只做配置解析、装配和 readiness 级检查，不执行 recall、ingestion 或真实 Provider 请求。

第一版不管理 Python 包安装、升级和卸载；operator 负责依赖和 module 可导入性。

## 14. 迁移方案

### 阶段一：建立契约和 Host

- 新增 Memory Plugin contracts、factory、registry、host 和 assembly report。
- 保留 `LongTermMemoryService` 作为兼容 facade，内部委托 `MemoryPluginHost`。
- 不改变 Runtime、Gateway 和 API 的外部构造接口。

### 阶段二：迁移 Mem0

- 新增内置 `Mem0MemoryPlugin` 和 factory。
- 将当前 Mem0 recall/ingestion 转换逻辑收进 Plugin。
- `Mem0Client` 保持 Plugin 私有 adapter，不实现 Assistant Runtime 接口。
- 现有 snapshot store、ingestion queue 和 observability 中属于宿主治理的部分迁入 Host；Mem0 特有部分留在 Plugin。
- 删除 Runtime/Host 中的 Mem0 特有分支和配置读取。

### 阶段三：切换 Runtime 依赖

- Runtime 只依赖 `MemoryPluginHost` 或兼容 facade。
- 所有入口通过同一个 composition root 取得活动 Memory Plugin。
- 默认单 Agent、Gateway、CLI、demo 和 eval 行为保持一致。

### 阶段四：验证第三方扩展

- 提供仅测试使用的 `ExampleMultimodalMemoryPlugin`/fake factory。
- 用 text + managed image ref 验证第三方 Plugin 无需修改 Runtime 即可接入。
- 不连接真实第三方服务，不把 example Plugin 作为默认产品能力。

迁移完成后，`docs/memory-service-architecture.md` 应从“只支持 Mem0”更新为“只允许一个排他的 active Memory Plugin，Mem0 是默认内置实现”。Memory 仍不是默认模型可调用 Tool。

## 15. 测试与验证

临时 RED/GREEN 测试放入独立 `tests/tdd/<feature>/`；确认属于长期架构不变量的契约再进入 `tests/core`。至少保护：

1. 只能选择一个活动 Memory slot。
2. descriptor、module、factory、配置和 slot 错误在启动期 fail closed。
3. 用户 metadata 不能覆盖 Plugin identity。
4. Plugin 不接收 `AgentState`、PromptCompiler 或可变消息列表。
5. `prepare_context()` 每个 user turn 最多一次，同一 run 使用冻结 contribution。
6. baseline 与本轮结果按 memory ID 确定性去重。
7. Plugin 结果经过 schema、大小、媒体和 prompt-safety 校验。
8. 无效或跨 owner media ref 被拒绝。
9. ingestion 使用稳定 idempotency key，并按 identity 串行。
10. ingestion 失败不改变已完成 Agent run。
11. Plugin 切换后旧 session handle 不复用。
12. mock/offline 不联网，不静默制造成功记忆。
13. `Mem0MemoryPlugin` 与当前默认 session recall、冻结 context、后台 ingestion 和观测行为等价。
14. fake 多模态 Plugin 能通过标准 API 读取受管 image ref，而不获得绝对路径。

全部默认验证使用 mock/fake Plugin，不调用真实 Mem0、真实第三方服务或真实 Provider。

## 16. 验收标准

设计完成实现后，应满足：

- Runtime 代码不直接依赖 `Mem0Client` 或 Mem0 HTTP/config 细节。
- Mem0 作为 `assistant_memory_plugin_v1` 的默认内置实现完成全部现有长期记忆行为。
- operator 可以显式配置一个受信任的 Python Memory Plugin module，并通过同一 Host 生命周期运行。
- 第三方 Plugin 能收到可信 opaque identity、标准消息和受管多模态引用，返回结构化 contribution。
- Plugin 无法通过正式 API 直接修改 prompt、扩大 Tool 权限或覆盖身份。
- Memory 失败保持可解释降级，默认回答路径继续工作。
- Plugin inventory、活动 slot、版本、readiness 和失败原因可只读诊断。
- 相关权威文档、测试导航和配置说明与实现保持一致。
