# Assistant Memory Plugin API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立原生 Python `assistant_memory_plugin_v1`，使 Runtime 只通过排他的 `MemoryPluginHost` 调用活动 Memory Plugin，并把现有 Mem0 行为迁移为默认内置 `Mem0MemoryPlugin`。

**Architecture:** 新增 Memory Plugin contracts、显式 factory/assembly/registry、受管媒体能力和 Host 生命周期；`LongTermMemoryService` 暂时保留为兼容 facade。Host 掌管身份、超时、取消、队列、返回校验、context 投影和审计，Plugin 掌管记忆算法及第三方服务交互；`Mem0Client` 只作为 `Mem0MemoryPlugin` 的私有 adapter。

**Tech Stack:** Python 3.11、Pydantic v2、标准库 `importlib`/`json`/`hashlib`/`concurrent.futures`、现有 `AgentGraphRuntime`、`ContextBuilder`、`MemoryIngestionQueue`、pytest。

## Global Constraints

- API 版本固定为 `assistant_memory_plugin_v1`，Plugin `kind` 固定为 `memory`。
- 同一 Runtime 只允许一个排他的活动 Memory Plugin；显式无效 slot 必须 fail closed。
- 第一版只支持 operator 显式配置的受信任进程内 Python Plugin，不扫描目录、不自动启用、不兼容运行 OpenClaw TypeScript Plugin。
- Runtime/Host 保留身份绑定、授权、媒体访问、context budget、安全投影、后台副作用调度和审计；Plugin 不接收可变 `AgentState`、PromptCompiler、ToolRegistry、Auth provider、TraceStore 或 EventSink。
- Plugin 只返回结构化 Memory item，不能返回 prompt role、prompt patch、绝对路径、凭据或 inline Base64 media。
- Memory 仍不是默认模型可调用 Tool；不得新增 `memory_search`、`memory_get` 或 `memory_save` Tool。
- Mem0 必须成为默认内置 `Mem0MemoryPlugin`；`Mem0Client` 只保留在该 Plugin 私有实现内，Runtime 不得读取 Mem0 HTTP/config 细节。
- `prepare_context()` 每个 user turn 最多调用一次；同一 Agent run 的所有 ReAct iteration 使用冻结 contribution。
- Turn ingestion 在 `response.delivered` 后进入有界后台队列；失败不改变已经完成的 Agent run。
- 多模态只通过 owner/session 绑定的 `ManagedMediaRef` 和受控 reader/writer 访问；默认不传绝对路径或 Base64。
- 默认 pytest、TDD 和 CLI 检查全部使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Mem0、第三方服务或 Provider。
- 不新增依赖，不写入或提交 secret、真实配置、真实用户记忆或媒体。
- Core invariant：扩展 `EXT-001` 以覆盖声明式 Memory Plugin 装配；`DUR-001`、`OBS-001` 的后台与观测契约保持不变并迁移现有测试。临时 RED/GREEN 只放在 `tests/tdd/memory-plugin-api/`，用户可手动整目录删除。

---

## File Structure

### 新增源码

- `src/assistant_agent/memory/plugins/__init__.py`：轻量 package marker，不聚合具体实现。
- `src/assistant_agent/memory/plugins/contracts.py`：descriptor、capabilities、请求/结果、`ManagedMediaRef`、factory/build context、assembly report。
- `src/assistant_agent/memory/plugins/config.py`：本机 JSON 配置、`${ENV_NAME}` SecretRef 解析和兼容默认配置。
- `src/assistant_agent/memory/plugins/assembly.py`：显式 module 导入、factory 校验、单 slot 选择和原子装配。
- `src/assistant_agent/memory/plugins/registry.py`：sealed inventory、活动 Plugin 和 generation。
- `src/assistant_agent/memory/plugins/media.py`：owner-bound media store、reader/writer 和请求引用解析；复用 contracts 中的 `ManagedMediaRef`。
- `src/assistant_agent/memory/plugins/session_store.py`：Host 私有 session handle、baseline 和活动 Plugin identity 状态。
- `src/assistant_agent/memory/plugins/host.py`：open/prepare/ingest/close 生命周期、校验、重试、队列和降级。
- `src/assistant_agent/memory/plugins/builtin/__init__.py`：内置 Memory Plugin package marker。
- `src/assistant_agent/memory/plugins/builtin/mem0.py`：`Mem0MemoryPlugin` 和 factory，拥有 `Mem0Client`。
- `src/assistant_agent/memory/cli.py`：只读 `plugins` 装配报告。

### 修改源码

- `src/assistant_agent/memory/models.py`：让 session snapshot 承载标准 Memory context item，并保留 Mem0 client 私有兼容模型。
- `src/assistant_agent/memory/service.py`：从直接编排 Mem0 改为兼容 facade，委托 `MemoryPluginHost`。
- `src/assistant_agent/memory/factory.py`：统一创建配置、media store、registry、Host、facade。
- `src/assistant_agent/memory/mem0/client.py`：只接受/返回 Mem0 私有模型，不再绑定 Runtime identity。
- `src/assistant_agent/memory/mem0/identity.py`：把现有稳定 hash 逻辑改成 Host 可复用的 Plugin-scoped identity helper，保持 Mem0 ID 不漂移。
- `src/assistant_agent/memory/observability.py`：增加 plugin identity、operation、status、latency、count、issue code 等安全属性。
- `src/assistant_agent/runtime/runtime.py`：每个 run 在首次模型调用前准备一次 Memory context；继续在 response delivered 后排队写入。
- `src/assistant_agent/runtime/state.py`：记录本 run 是否已冻结 Memory contribution。
- `src/assistant_agent/config/__init__.py`：增加配置路径和 Host 执行/资源上限，保留现有 Mem0 env 兼容。
- `src/assistant_agent/context/builder.py`：消费标准 Memory item，并保持原有不可信历史与预算编译语义。
- `src/assistant_agent/context/report.py`：把 source 从 Mem0/snapshot 特例改为活动 Memory Plugin 的通用来源。

### 测试与文档

- `tests/tdd/memory-plugin-api/test_contracts.py`
- `tests/tdd/memory-plugin-api/test_assembly.py`
- `tests/tdd/memory-plugin-api/test_managed_media.py`
- `tests/tdd/memory-plugin-api/test_host_lifecycle.py`
- `tests/tdd/memory-plugin-api/test_mem0_plugin.py`
- `tests/tdd/memory-plugin-api/test_runtime_integration.py`
- `tests/tdd/memory-plugin-api/test_cli.py`
- `tests/core/INVARIANTS.md`
- `tests/core/contract/test_extension_contract.py`
- `tests/core/integration/test_memory_lifecycle.py`
- `docs/memory-service-architecture.md`
- `README.md`
- `scripts/README.md`

---

### Task 1: 定义 `assistant_memory_plugin_v1` 强类型契约

**Files:**
- Create: `src/assistant_agent/memory/plugins/__init__.py`
- Create: `src/assistant_agent/memory/plugins/contracts.py`
- Modify: `src/assistant_agent/memory/models.py`
- Create: `tests/tdd/memory-plugin-api/test_contracts.py`

**Interfaces:**
- Consumes: 现有 `RequestIdentity`、`SessionMemorySnapshot`、`LongTermMemory` 和 Pydantic v2。
- Produces: `MemoryPluginDescriptor`、`MemoryPluginCapabilities`、`MemoryPlugin`、`MemoryPluginFactory`、`MemoryPluginBuildContext`、四组生命周期 request/result、`MemoryContextItem`、`MemoryContextContribution`、`MemoryPluginIssue`、`MemoryPluginExecutionPolicy`。

- [ ] **Step 1: 写 descriptor、冻结 request 和禁止 prompt patch 的 RED 测试**

```python
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from assistant_agent.memory.plugins.contracts import (
    MemoryBudgetHint,
    MemoryContextItem,
    MemoryContextRequest,
    MemoryIdentity,
    MemoryMessage,
    MemoryPluginCapabilities,
    MemoryPluginDescriptor,
    NeverCancelledMemoryToken,
)


def test_memory_plugin_descriptor_is_versioned_and_memory_only() -> None:
    descriptor = MemoryPluginDescriptor(
        plugin_id="probe.memory",
        plugin_version="1",
        capabilities=MemoryPluginCapabilities(
            modalities={"text", "image"},
            supports_session_recall=True,
            supports_turn_ingestion=True,
            supports_context_refresh=True,
            supports_idempotent_ingestion=True,
        ),
    )
    assert descriptor.api_version == "assistant_memory_plugin_v1"
    assert descriptor.kind == "memory"


def test_memory_context_item_rejects_prompt_fields() -> None:
    with pytest.raises(ValidationError):
        MemoryContextItem.model_validate(
            {
                "memory_id": "memory-sentinel",
                "text": "memory-sentinel",
                "source": "long_term",
                "role": "system",
            }
        )


def test_context_request_is_frozen() -> None:
    request = MemoryContextRequest(
        memory_session_id="memory-session-sentinel",
        session_handle=None,
        identity=MemoryIdentity(
            user_id="user-sentinel",
            agent_id="agent-sentinel",
            session_id="session-sentinel",
        ),
        current_turn=MemoryMessage(role="user", text="request-sentinel"),
        media_refs=[],
        context_budget_hint=MemoryBudgetHint(max_items=8, max_chars=2048),
        deadline=datetime.now(timezone.utc),
        cancellation=NeverCancelledMemoryToken(),
    )
    with pytest.raises(ValidationError):
        request.memory_session_id = "changed"
```

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_contracts.py
```

Expected: collection FAIL，提示 `assistant_agent.memory.plugins.contracts` 不存在。

- [ ] **Step 3: 实现完整 v1 模型与 Protocol**

在 `contracts.py` 中使用 `ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)` 定义 request/result；核心声明必须与 spec 一致：

```python
class MemoryCancellationToken(Protocol):
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...


@dataclass(frozen=True)
class NeverCancelledMemoryToken:
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class MemoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=20_000)


class MemoryToolEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_name: str = Field(min_length=1, max_length=128)
    status: Literal["succeeded", "failed", "partial"]
    output_ref: str | None = Field(default=None, max_length=512)


class MemoryBudgetHint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_items: int = Field(ge=0)
    max_chars: int = Field(ge=0)


class MemoryPlugin(Protocol):
    descriptor: MemoryPluginDescriptor

    def open_session(
        self, request: MemorySessionOpenRequest
    ) -> MemorySessionOpenResult: ...

    def prepare_context(
        self, request: MemoryContextRequest
    ) -> MemoryContextContribution: ...

    def ingest_turn(
        self, request: MemoryTurnIngestionRequest
    ) -> MemoryTurnIngestionResult: ...

    def close_session(
        self, request: MemorySessionCloseRequest
    ) -> MemorySessionCloseResult: ...


@dataclass(frozen=True)
class MemoryPluginBuildContext:
    provider_mode: Literal["mock", "real"]
    media_reader: MemoryMediaReader
    artifact_writer: MemoryArtifactWriter
    secret_resolver: MemorySecretResolver
    clock: Callable[[], datetime]


class MemoryPluginFactory(Protocol):
    descriptor: MemoryPluginDescriptor
    config_model: type[BaseModel]

    def build(
        self,
        context: MemoryPluginBuildContext,
        config: BaseModel,
    ) -> MemoryPlugin: ...
```

同一文件还要定义 `ManagedMediaRef`、`CompletedMemoryTurn`、`MemoryChange` 和四组 request/result；字段逐字采用已批准 spec 第 7 节。`MemoryContextItem` 只允许 `memory_id/text/source/relevance/occurred_at/created_at/media_refs/metadata`；`MemoryContextContribution` 只允许 `items/status/issues`。`MemoryPluginExecutionPolicy` 给出明确默认值：open 5 秒、prepare 5 秒、ingest 30 秒、close 5 秒、最多 10,000 items、2,000,000 chars、每 turn 16 个媒体、32 MiB。

在 `memory/models.py` 中让 `SessionMemorySnapshot.memories` 接受标准 `MemoryContextItem`，并增加 `plugin_id/status/error_codes`；让兼容 `LongTermMemory` 继承 `MemoryContextItem` 且固定默认 `source="long_term"`，使当前 Mem0 client 输出仍可验证；暂时保留旧 `CompletedTurn` 供 Task 6 迁移，不删除旧类型。

- [ ] **Step 4: 补齐 schema 边界测试并运行 GREEN**

增加断言：未知 field、非法 relevance、空 memory ID、非法 source、非 JSON metadata 均被拒绝；`MemoryPluginIssue.message` 有长度上限，`session_handle` 有长度上限。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_contracts.py
```

Expected: PASS。

- [ ] **Step 5: 提交契约**

```bash
git add src/assistant_agent/memory/plugins src/assistant_agent/memory/models.py \
  tests/tdd/memory-plugin-api/test_contracts.py
git commit -m "feat(memory): define plugin api contracts"
```

---

### Task 2: 实现显式配置、Factory 装配和排他 Registry

**Files:**
- Create: `src/assistant_agent/memory/plugins/config.py`
- Create: `src/assistant_agent/memory/plugins/assembly.py`
- Create: `src/assistant_agent/memory/plugins/registry.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Create: `tests/tdd/memory-plugin-api/test_assembly.py`

**Interfaces:**
- Consumes: Task 1 的 `MemoryPluginFactory`、`MemoryPluginDescriptor`、`MemoryPluginBuildContext`。
- Produces: `MemoryPluginsConfig`、`MemoryPluginEntryConfig`、`load_memory_plugins_config()`、`assemble_memory_plugins()`、`MemoryPluginRegistry.active_plugin`、`MemoryPluginAssemblyReport`。

- [ ] **Step 1: 写单 slot、显式 module 和 SecretRef 的 RED 测试**

```python
import json
import sys
from datetime import datetime, timezone
from types import ModuleType

import pytest

from assistant_agent.memory.plugins.assembly import (
    MemoryPluginAssemblyError,
    assemble_memory_plugins,
)
from assistant_agent.memory.plugins.config import load_memory_plugins_config
from assistant_agent.memory.plugins.config import MemoryPluginsConfig
from assistant_agent.memory.plugins.contracts import MemoryPluginBuildContext


def _config(*, slot: str, plugins: dict) -> MemoryPluginsConfig:
    return MemoryPluginsConfig(
        schema_version="assistant_memory_plugins_v1",
        slot=slot,
        plugins=plugins,
    )


def _build_context() -> MemoryPluginBuildContext:
    return MemoryPluginBuildContext(
        provider_mode="mock",
        media_reader=object(),
        artifact_writer=object(),
        secret_resolver=object(),
        clock=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def test_config_resolves_declared_env_reference_only(tmp_path) -> None:
    path = tmp_path / "memory_plugins.json"
    path.write_text(json.dumps({
        "schema_version": "assistant_memory_plugins_v1",
        "slot": "probe.memory",
        "plugins": {
            "probe.memory": {
                "enabled": True,
                "module": "probe_memory_plugin",
                "config": {"api_key": "${PROBE_MEMORY_API_KEY}"},
            }
        },
    }), encoding="utf-8")
    config = load_memory_plugins_config(
        path,
        env={"PROBE_MEMORY_API_KEY": "secret-sentinel"},
    )
    assert config.slot == "probe.memory"
    assert config.plugins["probe.memory"].config["api_key"].get_secret_value() == "secret-sentinel"


def test_unknown_active_slot_fails_closed(tmp_path) -> None:
    with pytest.raises(MemoryPluginAssemblyError) as exc_info:
        assemble_memory_plugins(
            config=_config(slot="missing.memory", plugins={}),
            builtin_factories=(),
            build_context=_build_context(),
        )
    assert exc_info.value.report.issues[0].code == "memory_plugin_slot_unknown"
```

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_assembly.py
```

Expected: FAIL，配置和 assembly module 尚不存在。

- [ ] **Step 3: 实现配置模型与安全解析**

```python
MEMORY_PLUGIN_CONFIG_PATH_ENV = "MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH"
MEMORY_PLUGIN_EXPORT = "__assistant_memory_plugin_factory__"


class MemoryPluginEntryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    module: str
    config: dict[str, JsonValue | SecretStr] = Field(default_factory=dict)


class MemoryPluginsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["assistant_memory_plugins_v1"]
    slot: str
    plugins: dict[str, MemoryPluginEntryConfig]
```

解析器只接受完整 `${[A-Za-z_][A-Za-z0-9_]*}` 引用；缺失引用返回 `memory_plugin_secret_missing`，错误信息不得包含 secret。没有新配置文件时由 composition root 生成选择内置 `mem0` 的兼容配置，不在 loader 中读取真实 `.env`。

在 `ProviderConfig` 增加：

```python
memory_plugin_config_path: str | None = None
memory_plugin_open_timeout_seconds: float = 5.0
memory_plugin_prepare_timeout_seconds: float = 5.0
memory_plugin_ingest_timeout_seconds: float = 30.0
memory_plugin_close_timeout_seconds: float = 5.0
```

`from_env()` 只读取对应显式环境变量，保持 mock mode 不读取或验证 Plugin secret。

- [ ] **Step 4: 实现原子 assembly 和 sealed Registry**

`assemble_memory_plugins()` 必须：验证内置和配置 module factory descriptor；禁止 duplicate ID；先校验所有 candidate，再只构造活动 slot；校验 factory 返回 Plugin 的 descriptor 与 factory descriptor 相等；构造失败不留下 Registry。

```python
class MemoryPluginRegistry:
    def __init__(self, records: list[MemoryPluginRegistrationRecord], active_plugin: MemoryPlugin): ...

    @property
    def active_plugin(self) -> MemoryPlugin: ...

    @property
    def generation(self) -> str: ...

    @property
    def assembly_report(self) -> MemoryPluginAssemblyReport: ...
```

generation 使用 descriptor、source 和活动 slot 的 canonical JSON SHA-256；report 不包含 Plugin object、secret 或 config value。

- [ ] **Step 5: 覆盖 duplicate、禁用 slot、错误 export、descriptor mismatch 并运行 GREEN**

使用 `ModuleType` 放入 `sys.modules`，分别断言稳定 issue code：

```text
memory_plugin_duplicate_id
memory_plugin_slot_disabled
memory_plugin_export_missing
memory_plugin_descriptor_mismatch
memory_plugin_build_failed
```

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_assembly.py
```

Expected: PASS。

- [ ] **Step 6: 提交装配边界**

```bash
git add src/assistant_agent/memory/plugins/config.py \
  src/assistant_agent/memory/plugins/assembly.py \
  src/assistant_agent/memory/plugins/registry.py \
  src/assistant_agent/config/__init__.py \
  tests/tdd/memory-plugin-api/test_assembly.py
git commit -m "feat(memory): assemble exclusive memory plugins"
```

---

### Task 3: 建立 owner-bound 多模态引用和受控 Reader/Writer

**Files:**
- Create: `src/assistant_agent/memory/plugins/media.py`
- Modify: `src/assistant_agent/memory/plugins/contracts.py`
- Create: `tests/tdd/memory-plugin-api/test_managed_media.py`

**Interfaces:**
- Consumes: Task 1 的 `ManagedMediaRef` forward contract、`MemoryIdentity`，现有 `UserRequest.image_ids/video_ids/audio_id`。
- Produces: `ManagedMemoryMediaStore.register()`、`.resolve_request_refs()`、`.read()`、`.open_stream()`；`MemoryPluginBuildContext.media_reader/artifact_writer` 使用同一受管 store。

- [ ] **Step 1: 写 owner、大小和过期拒绝 RED 测试**

```python
from datetime import datetime, timedelta, timezone

import pytest

from assistant_agent.memory.plugins.media import (
    ManagedMemoryMediaStore,
    MemoryMediaAccessError,
)


def test_managed_media_read_requires_matching_owner() -> None:
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        owner_scope="user-a:agent-a:session-a",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"jpeg-sentinel",
    )
    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.read(ref, owner_scope="user-b:agent-a:session-a", max_bytes=1024)
    assert exc_info.value.code == "memory_media_owner_mismatch"


def test_managed_media_read_enforces_call_limit() -> None:
    store = ManagedMemoryMediaStore(max_total_bytes=1024)
    ref = store.register(
        owner_scope="owner-sentinel",
        media_type="image",
        mime_type="image/jpeg",
        payload=b"123456",
    )
    with pytest.raises(MemoryMediaAccessError) as exc_info:
        store.read(ref, owner_scope="owner-sentinel", max_bytes=5)
    assert exc_info.value.code == "memory_media_size_limit"
```

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_managed_media.py
```

Expected: FAIL，`memory.plugins.media` 尚不存在。

- [ ] **Step 3: 实现内存受管 store 和引用解析**

store 只保存 Host 登记的 bytes，不接受路径。`register()` 生成 opaque `ref_id`；`read()`/`open_stream()` 重验 owner、mime、modality、过期、per-call bytes；`resolve_request_refs()` 只解析 store 中已存在且 owner 匹配的 `image_ids/video_ids/audio_id`，忽略调用方伪造 ID 并返回结构化 issue。

```python
def resolve_request_refs(
    self,
    request: UserRequest,
    *,
    owner_scope: str,
    allowed_modalities: set[MemoryModality],
    max_items: int,
    max_total_bytes: int,
) -> tuple[list[ManagedMediaRef], list[MemoryPluginIssue]]: ...
```

`MemoryArtifactWriter.register()` 复用同一个受管 store；不得接受 URL、Path 或 Base64 string。

- [ ] **Step 4: 增加 modality、伪造 ID、总预算和 stream 测试并运行 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_managed_media.py
```

Expected: PASS；测试不得创建真实媒体文件。

- [ ] **Step 5: 提交媒体边界**

```bash
git add src/assistant_agent/memory/plugins/contracts.py \
  src/assistant_agent/memory/plugins/media.py \
  tests/tdd/memory-plugin-api/test_managed_media.py
git commit -m "feat(memory): add governed plugin media refs"
```

---

### Task 4: 实现 Host 的 session open、每 turn recall 和冻结 contribution

**Files:**
- Create: `src/assistant_agent/memory/plugins/session_store.py`
- Create: `src/assistant_agent/memory/plugins/host.py`
- Modify: `src/assistant_agent/memory/plugins/contracts.py`
- Modify: `src/assistant_agent/memory/models.py`
- Modify: `src/assistant_agent/runtime/state.py`
- Create: `tests/tdd/memory-plugin-api/test_host_lifecycle.py`

**Interfaces:**
- Consumes: Task 2 的 `MemoryPluginRegistry`，Task 3 的 media store，现有 `SessionMemorySnapshot`、`AgentState`。
- Produces: `MemoryPluginSessionRecord`、`MemoryPluginSessionStore`、`MemoryPluginHost.open_session()`、`.prepare_context()`、`.attach_frozen_context()`。

- [ ] **Step 1: 写 open 一次、prepare 一次和 deterministic merge 的 RED 测试**

```python
def test_host_prepares_memory_once_per_run() -> None:
    plugin = RecordingMemoryPlugin(
        baseline=[_item("shared", "baseline")],
        current=[_item("shared", "current"), _item("turn", "turn")],
    )
    host = _host(plugin)
    state = _state(run_id="run-sentinel")

    host.open_session(identity=_identity(), state=state, trace_store=None)
    first = host.prepare_context(state=state, trace_store=None, cancel_token=None)
    second = host.prepare_context(state=state, trace_store=None, cancel_token=None)

    assert plugin.open_calls == 1
    assert plugin.prepare_calls == 1
    assert first == second
    assert [(item.memory_id, item.text) for item in first.memories] == [
        ("shared", "current"),
        ("turn", "turn"),
    ]
```

- [ ] **Step 2: 运行 Host RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_host_lifecycle.py -k 'open or prepare or merge'
```

Expected: FAIL，Host/session store 尚不存在。

- [ ] **Step 3: 实现 Plugin-scoped identity 和 session store**

将现有稳定 hash 抽成 Host helper：

```python
def bind_memory_plugin_identity(
    identity: RequestIdentity,
    *,
    namespace: str,
) -> MemoryIdentity:
    ...
```

对默认 Mem0 namespace 必须产生与当前 `bind_mem0_identity()` 完全相同的 `usr_`、`agt_`、`run_` 值，防止现有记忆身份漂移。

`MemoryPluginSessionRecord` 保存 plugin ID/version、runtime identity key、plugin-scoped identity、memory session ID、handle、baseline、status；store 按 `(user_id, agent_id, session_id)` 隔离，提供 `resolve/get/clear_session/clear_user`。

- [ ] **Step 4: 实现 open/prepare、调用 deadline 和返回值 validator**

Host 的公开签名：

```python
def open_session(
    self,
    *,
    identity: RequestIdentity,
    state: AgentState,
    trace_store: TraceStore | None,
    reset: bool = False,
) -> SessionMemorySnapshot: ...

def prepare_context(
    self,
    *,
    state: AgentState,
    trace_store: TraceStore | None,
    cancel_token: Any | None,
) -> SessionMemorySnapshot: ...
```

`prepare_context()` 使用 `state.memory_context_prepared` 防止同 run 重入；baseline 与当前 contribution 按 ID 去重，当前 item 覆盖 baseline 并保留确定顺序。`supports_context_refresh=false` 时不调用 Plugin prepare。异常、timeout、cancel 或无效结果降级为空/已有 baseline，并记录稳定 issue code。

返回 validator 必须检查 item/count/chars、metadata JSON、media owner、绝对路径和 inline data；整体无效时拒绝 contribution，不部分注入，并记录稳定 code `memory_plugin_invalid_result`。

- [ ] **Step 5: 覆盖 identity 伪造、invalid result、timeout 和取消测试并运行 GREEN**

测试通过 `UserRequest.metadata` 放入伪造 identity，断言 Plugin 收到的仍是 Host 绑定值；用迟到 Plugin 断言 Host 丢弃结果；用 cancel token 断言 Plugin 收到可协作检查的 token。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_host_lifecycle.py
```

Expected: PASS。

- [ ] **Step 6: 提交读取生命周期**

```bash
git add src/assistant_agent/memory/plugins/contracts.py \
  src/assistant_agent/memory/plugins/session_store.py \
  src/assistant_agent/memory/plugins/host.py \
  src/assistant_agent/memory/models.py \
  src/assistant_agent/runtime/state.py \
  tests/tdd/memory-plugin-api/test_host_lifecycle.py
git commit -m "feat(memory): host plugin recall lifecycle"
```

---

### Task 5: 实现后台 ingestion、幂等、关闭和 Plugin 观测

**Files:**
- Modify: `src/assistant_agent/memory/plugins/host.py`
- Modify: `src/assistant_agent/memory/plugins/session_store.py`
- Modify: `src/assistant_agent/memory/observability.py`
- Modify: `src/assistant_agent/memory/ingestion_queue.py`
- Modify: `tests/tdd/memory-plugin-api/test_host_lifecycle.py`

**Interfaces:**
- Consumes: Task 4 的活动 session record 和 `MemoryPluginHost`。
- Produces: `MemoryPluginHost.schedule_ingestion()`、`.drain()`、`.close_session()`、`.clear_session()`、`.clear_user()`、`.close()`。

- [ ] **Step 1: 写 response 后排队、同身份串行和稳定 idempotency RED 测试**

```python
def test_host_schedules_completed_turn_with_stable_idempotency_key() -> None:
    plugin = BlockingIngestionMemoryPlugin()
    host = _host(plugin)
    state = _completed_state(run_id="run-sentinel", turn_index="2")
    host.open_session(identity=_identity(), state=state, trace_store=None)

    assert host.schedule_ingestion(state=state, trace_store=None) is True
    assert plugin.started.wait(0.5)
    assert plugin.requests[0].idempotency_key == plugin.requests[0].idempotency_key
    assert state.request.metadata["memory_ingestion"]["status"] == "queued"
```

另写测试：不支持幂等的 Plugin 失败后只调用一次；支持幂等且返回 recoverable issue 时最多重试一次；`close_session()` 重复调用只产生一个外部 close 副作用。

- [ ] **Step 2: 运行 ingestion RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_host_lifecycle.py -k 'ingestion or close or retry'
```

Expected: FAIL，Host 尚无写入/关闭实现。

- [ ] **Step 3: 实现标准 CompletedMemoryTurn 和后台调度**

Host 从完成的 `AgentState` 构造标准消息、prompt-safe tool evidence 和当前受管 media refs；idempotency key 使用 `sha256(plugin_id + run_id + conversation_turn_index)`。继续保留当前结构化跳过规则，例如全部 ToolResult 都是 `visual_reminder_manage` 时不入队，不读取用户文本判断意图。

复用 `MemoryIngestionQueue` 的 per-identity ordering key 和全局 pending bound；队列 callback 只调用 `plugin.ingest_turn()`，不保存可变 `AgentState` 的长期引用。

- [ ] **Step 4: 实现 close/clear 和安全观测**

`close_session()` 先停止接受该 session 新 ingestion，有界 drain 已接收任务，再调用 Plugin；清除 handle、baseline 和 run freeze。`clear_session()`/`clear_user()` 只清理 Host session 状态，不新增远端记忆 CRUD。

扩展 `record_session_recall()`/`record_ingestion_*()` 或新增通用 helper，使 canonical event 保持现有名称，同时 attributes 增加：

```text
memory_plugin_id
memory_plugin_version
memory_plugin_api_version
memory_plugin_operation
memory_plugin_issue_codes
memory_plugin_retry_count
```

禁止记录 handle、正文、媒体、secret 和远端原始错误。

- [ ] **Step 5: 运行 Host lifecycle 全集 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_host_lifecycle.py
```

Expected: PASS。

- [ ] **Step 6: 提交写入和关闭生命周期**

```bash
git add src/assistant_agent/memory/plugins/host.py \
  src/assistant_agent/memory/plugins/session_store.py \
  src/assistant_agent/memory/observability.py \
  src/assistant_agent/memory/ingestion_queue.py \
  tests/tdd/memory-plugin-api/test_host_lifecycle.py
git commit -m "feat(memory): govern plugin ingestion lifecycle"
```

---

### Task 6: 将现有 Mem0 迁移为默认内置 Plugin

**Files:**
- Create: `src/assistant_agent/memory/plugins/builtin/__init__.py`
- Create: `src/assistant_agent/memory/plugins/builtin/mem0.py`
- Modify: `src/assistant_agent/memory/mem0/client.py`
- Modify: `src/assistant_agent/memory/mem0/identity.py`
- Modify: `src/assistant_agent/memory/mem0/models.py`
- Modify: `src/assistant_agent/memory/service.py`
- Modify: `src/assistant_agent/memory/factory.py`
- Create: `tests/tdd/memory-plugin-api/test_mem0_plugin.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`

**Interfaces:**
- Consumes: Task 1–5 的 factory、contracts、Host 和 Plugin-scoped identity。
- Produces: `Mem0MemoryPlugin`、`Mem0MemoryPluginFactory`、`default_memory_plugin_factories()`、兼容 `LongTermMemoryService` facade、更新后的 `create_long_term_memory_service()`。

- [ ] **Step 1: 写 Mem0 Plugin 映射和旧 identity 稳定性的 RED 测试**

```python
def test_mem0_plugin_open_session_maps_native_records() -> None:
    client = RecordingMem0Client(
        memories=[LongTermMemory(
            memory_id="memory-sentinel",
            text="fact-sentinel",
            created_at=NOW,
        )]
    )
    plugin = Mem0MemoryPlugin(client=client)
    result = plugin.open_session(_open_request())

    assert result.status == "ready"
    assert result.initial_contribution.items[0].memory_id == "memory-sentinel"
    assert result.initial_contribution.items[0].source == "long_term"


def test_mem0_plugin_identity_matches_legacy_binding() -> None:
    runtime_identity = RequestIdentity.for_user(
        user_id="user-sentinel",
        agent_id="agent-sentinel",
        session_id="session-sentinel",
    )
    assert bind_memory_plugin_identity(
        runtime_identity,
        namespace="assistant-agent",
    ).model_dump() == legacy_mem0_identity_payload(runtime_identity)
```

- [ ] **Step 2: 运行 Mem0 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_mem0_plugin.py
```

Expected: FAIL，内置 Mem0 Plugin 尚不存在。

- [ ] **Step 3: 实现 `Mem0MemoryPlugin`**

descriptor：`plugin_id="mem0"`、version `1`、modalities 仅 `text`、session recall/turn ingestion/idempotent ingestion 为 true、context refresh 为 false。

```python
class Mem0MemoryPlugin:
    def __init__(self, client: Mem0Client | UnavailableMem0Client) -> None:
        self._client = client

    def open_session(self, request: MemorySessionOpenRequest) -> MemorySessionOpenResult:
        ...

    def prepare_context(self, request: MemoryContextRequest) -> MemoryContextContribution:
        return MemoryContextContribution(status="succeeded", items=[])

    def ingest_turn(self, request: MemoryTurnIngestionRequest) -> MemoryTurnIngestionResult:
        ...

    def close_session(self, request: MemorySessionCloseRequest) -> MemorySessionCloseResult:
        return MemorySessionCloseResult(status="closed")
```

`Mem0Client` 改为接受 Plugin 已绑定的 Mem0 identity/private turn model，不再导入 Runtime `RequestIdentity`。保持 `/memories` GET/POST、分页、30 秒最小 ingestion timeout、ADD/UPDATE/DELETE 映射和错误语义不变。

- [ ] **Step 4: 把 `LongTermMemoryService` 改为 Host facade**

保持现有公开方法名，内部全部委托：

```python
class LongTermMemoryService:
    def __init__(self, *, host: MemoryPluginHost) -> None:
        self.host = host

    def initialize_session(...):
        return self.host.open_session(...)

    def prepare_context(...):
        return self.host.prepare_context(...)

    def enqueue_completed_turn(...):
        return self.host.schedule_ingestion(...)
```

同时委托 `clear_session/clear_user/drain/close`，删除 service 中的 `self.client` 和 Mem0 特有逻辑。

- [ ] **Step 5: 更新 factory 的兼容 composition root**

`create_long_term_memory_service()` 在无新配置文件时装配内置 Mem0 factory；real + `MEM0_BASE_URL` 创建真实 client，其他情况创建 `UnavailableMem0Client`，不联网、不回退其他真实 Plugin。snapshot/session store、queue、execution policy、media store 都由 composition root 构造并注入 Host。

- [ ] **Step 6: 同步迁移现有 `OBS-001` Memory lifecycle fixture**

把 `BlockingIngestionClient + LongTermMemoryService(client=...)` 改成实现标准 API 的 `BlockingMemoryPlugin + MemoryPluginHost`。保留原断言：Runtime 在后台写入完成前返回，`response.delivered` 先于 `memory.ingestion.queued`，最终 `memory.ingestion.finished` 带成功状态。`DUR-001` 的 queue 串并行测试不改私有断言和 marker。

- [ ] **Step 7: 运行 Mem0 Plugin 和现有 Memory 定向测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_mem0_plugin.py \
  tests/core/integration/test_memory_lifecycle.py
```

Expected: 全部 PASS；不允许把构造签名失败留给后续任务。

- [ ] **Step 8: 提交 Mem0 Plugin 迁移**

```bash
git add src/assistant_agent/memory/plugins/builtin \
  src/assistant_agent/memory/mem0 \
  src/assistant_agent/memory/service.py \
  src/assistant_agent/memory/factory.py \
  tests/tdd/memory-plugin-api/test_mem0_plugin.py \
  tests/core/integration/test_memory_lifecycle.py
git commit -m "refactor(memory): run mem0 behind plugin api"
```

---

### Task 7: 接入 Runtime、Context 和 Gateway 兼容入口

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/state.py`
- Modify: `src/assistant_agent/context/builder.py`
- Modify: `src/assistant_agent/context/report.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/gateway/runtime_pool.py`
- Create: `tests/tdd/memory-plugin-api/test_runtime_integration.py`

**Interfaces:**
- Consumes: Task 6 的兼容 `LongTermMemoryService.prepare_context()` 和现有 `initialize_session_memory()`。
- Produces: 每个 run 一次的 `_prepare_run_memory_context()`、冻结 `AgentState.session_memory_snapshot`、保持 Gateway session 初始化/清理行为。

- [ ] **Step 1: 写多 iteration 只召回一次和 ingestion 因果顺序 RED 测试**

```python
def test_runtime_prepares_plugin_context_once_across_react_iterations() -> None:
    plugin = RecordingMemoryPlugin(current=[_item("memory-sentinel", "fact-sentinel")])
    runtime = _runtime_with_plugin(
        plugin,
        chat_results=[_tool_call_result(), _final_result("response-sentinel")],
    )
    runtime.initialize_session_memory(_identity())
    state = runtime.run_state(_request())

    assert state.status == "completed"
    assert plugin.prepare_calls == 1
    assert state.session_memory_snapshot.memories[0].memory_id == "memory-sentinel"
    assert _compiled_memory_messages(runtime) == ["fact-sentinel"]
```

同一测试文件增加多模态贯通测试：先通过 `ManagedMemoryMediaStore.register()` 为当前 owner 登记 JPEG bytes，把返回 `ref_id` 放入 `UserRequest.image_ids`，断言 Plugin 的 `MemoryContextRequest.media_refs` 只含这一条 owner-bound image ref；再使用同一 ref 构造另一 user 的 request，断言 Plugin 收不到该 ref 且 Runtime 继续完成。另写测试断言 canonical event 顺序仍为 `run.completed < response.delivered < memory.ingestion.queued`。

- [ ] **Step 2: 运行 Runtime RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_runtime_integration.py
```

Expected: FAIL，Runtime 仍只 attach session snapshot，未调用 per-turn prepare。

- [ ] **Step 3: 在 run 创建后、ContextBuilder 前准备一次 Memory**

在 `AgentGraphRuntime.run_state()` 构造 `AgentState` 后调用：

```python
self.long_term_memory_service.prepare_context(
    state=state,
    trace_store=self.trace_store,
    cancel_token=cancel_token,
)
```

删除旧 `_attach_session_memory_snapshot()` 的重复职责，或把它收窄为上述 facade 调用。不得在每次 assistant loop node/iteration 中重新召回。

Runtime 完成路径继续在发布 `response.delivered` 后调用 `enqueue_completed_turn()`；取消/失败 run 不写入。`drain_memory_ingestions()` 和 `close()` 继续委托 facade。

- [ ] **Step 4: 更新 ContextBuilder 的通用 Memory 投影**

ContextBuilder 从标准 `MemoryContextItem` 读取 `memory_id/text`，维持独立不可信历史 JSON/context message、memory budget 和 source ID；不把 `metadata` 或媒体 bytes 自动展开到 prompt。Context report 的 source 改为 `MemoryPluginHost.active_plugin`，不出现 `ContextBuilder.session_memory_snapshot` 的 Mem0 特例命名。

- [ ] **Step 5: 保持 API/Gateway session clear 兼容**

`AssistantRuntimeApp.reset_session/delete_user` 和 `GatewayRuntimePool.initialize_session_memory()` 保持公开签名不变；内部 clear 触发 Host `close_session(reason="reset")` 并清除 session record，不能删除远端长期记忆。

- [ ] **Step 6: 运行 Runtime GREEN 和相关 Context/Gateway 定向测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_runtime_integration.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/contract/test_gateway_contract.py
```

Expected: PASS。

- [ ] **Step 7: 提交 Runtime 接入**

```bash
git add src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/runtime/state.py \
  src/assistant_agent/context/builder.py \
  src/assistant_agent/context/report.py \
  src/assistant_agent/runtime/assistant_runtime_app.py \
  src/assistant_agent/gateway/runtime_pool.py \
  tests/tdd/memory-plugin-api/test_runtime_integration.py
git commit -m "feat(memory): integrate plugin host with runtime"
```

---

### Task 8: 增加只读 CLI 和同步权威文档

**Files:**
- Create: `src/assistant_agent/memory/cli.py`
- Create: `tests/tdd/memory-plugin-api/test_cli.py`
- Modify: `docs/memory-service-architecture.md`
- Modify: `README.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: Task 2 的 assembly report、Task 6 的默认 factory/composition root。
- Produces: `python -m assistant_agent.memory.cli plugins` 和与实现一致的 Memory Plugin 架构说明。

- [ ] **Step 1: 写 CLI 不调用 Plugin 生命周期的 RED 测试**

```python
def test_plugins_cli_reports_selected_plugin_without_runtime_calls(
    monkeypatch, capsys
) -> None:
    factory = RecordingFactory()
    exit_code = memory_cli.main(["plugins"], factory_overrides=[factory])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "memory_plugin_assembly_v1"
    assert payload["active_slot"] == "probe.memory"
    assert payload["sealed"] is True
    assert factory.plugin.open_calls == 0
    assert factory.plugin.prepare_calls == 0
    assert factory.plugin.ingest_calls == 0
```

- [ ] **Step 2: 运行 CLI RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_cli.py
```

Expected: FAIL，`assistant_agent.memory.cli` 尚不存在。

- [ ] **Step 3: 实现只读 `plugins` 命令**

CLI 只解析配置、装配 factory、输出活动 slot、descriptor、source、selected、readiness、issues、generation 和 sealed；不得调用 `open_session/prepare_context/ingest_turn/close_session`，不得输出 config 或 secret。

失败输出同一 JSON schema，`sealed=false`、generation 为 null，并返回 exit code 1。

- [ ] **Step 4: 更新权威文档和导航**

`docs/memory-service-architecture.md` 必须改写：

```text
Runtime 只允许一个排他的 active Memory Plugin。
Mem0 是默认内置实现；Mem0Client 是 Mem0MemoryPlugin 的私有 adapter。
Memory Plugin API 与 Tool Plugin API 是两条独立边界。
```

同步记录 open/prepare/ingest/close、单 run freeze、受管 media、配置、CLI、mock/real 和失败降级。README 将“Mem0 memory architecture”改为“Memory Plugin architecture（默认 Mem0）”；scripts README 仅保留 operator Mem0 控制台，不把它描述成 Runtime Plugin 管理入口。

- [ ] **Step 5: 运行 CLI GREEN 和文档格式检查**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api/test_cli.py

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m assistant_agent.memory.cli plugins

git diff --check
```

Expected: pytest PASS；CLI 输出合法 JSON、active slot 为 `mem0`、不联网；diff check 无错误。

- [ ] **Step 6: 提交 CLI 和文档**

```bash
git add src/assistant_agent/memory/cli.py \
  tests/tdd/memory-plugin-api/test_cli.py \
  docs/memory-service-architecture.md README.md scripts/README.md
git commit -m "docs(memory): expose plugin diagnostics and architecture"
```

---

### Task 9: 固化 core invariant、迁移核心测试并完成全量离线验证

**Files:**
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/contract/test_extension_contract.py`

**Interfaces:**
- Consumes: 完整 `assistant_memory_plugin_v1`、Host、Mem0 Plugin、Runtime integration。
- Produces: 更新后的 `EXT-001` 稳定扩展契约；继续通过 `DUR-001`/`OBS-001` 保护后台和观测因果。

- [ ] **Step 1: 更新 `EXT-001` 登记而不新增重复 invariant**

将结构化契约改为：

```text
EXT-001 | Probe Tool 与受信任 capability Plugin 通过声明的 identity、版本、schema、显式装配和宿主治理契约接入；扩展不能绕过其所属治理链。 | tests/core/contract/test_extension_contract.py
```

不新增具体 Mem0、媒体服务或 CLI invariant。

- [ ] **Step 2: 在现有 extension contract 增加通用 Probe Memory Plugin**

测试只使用无语义 sentinel 和 fake factory，不导入具体 Mem0：

```python
@pytest.mark.core_invariant("EXT-001")
def test_probe_memory_plugin_assembles_single_slot_and_runs_through_host() -> None:
    assembly = assemble_memory_plugins(
        config=_probe_config(),
        builtin_factories=(ProbeMemoryPluginFactory(),),
        build_context=_offline_memory_build_context(),
    )
    host = _probe_memory_host(assembly.registry)
    state = AgentState.from_request(_probe_request(), run_id="run-sentinel")

    host.open_session(identity=_probe_identity(), state=state, trace_store=None)
    snapshot = host.prepare_context(
        state=state,
        trace_store=None,
        cancel_token=None,
    )

    assert assembly.registry.active_plugin.descriptor.plugin_id == "tests.memory_probe"
    assert snapshot.memories[0].memory_id == "memory-sentinel"
    assert snapshot.memories[0].text == "value-sentinel"
```

- [ ] **Step 3: 静态核对 Task 6 保留的 `OBS-001`/`DUR-001` Probe fixture**

Run:

```bash
rg -n "core_invariant\(\"(OBS-001|DUR-001)\"\)|BlockingMemoryPlugin|Mem0MemoryPlugin" \
  tests/core/integration/test_memory_lifecycle.py
```

Expected: 输出包含 `BlockingMemoryPlugin`、一个 `OBS-001` marker 和两个 `DUR-001` marker，不包含 `Mem0MemoryPlugin`。结构化行为由 Step 5 的 pytest 证明。

- [ ] **Step 4: 运行临时 TDD feature 全集**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/memory-plugin-api
```

Expected: PASS。该目录为临时 RED/GREEN，保留给用户手动删除，不自动晋升或删除。

- [ ] **Step 5: 运行定向 core invariant**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_extension_contract.py \
  tests/core/integration/test_memory_lifecycle.py \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_runtime_lifecycle.py
```

Expected: PASS。

- [ ] **Step 6: 运行共享核心基础设施的默认全量 pytest**

该变更修改 `EXT-001` 并触及 Runtime/Context/OBS/DUR，因此满足运行裸 pytest 的条件。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: PASS，且不访问网络或真实 Provider。

- [ ] **Step 7: 运行最终静态检查和装配 smoke**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m assistant_agent.memory.cli plugins

git diff --check
git status --short
```

Expected: CLI 报告 sealed `mem0` slot 且 readiness 为 unavailable/offline；diff check 无错误；status 只包含本任务文件和用户原有无关改动。

- [ ] **Step 8: 提交核心契约与验证迁移**

```bash
git add tests/core/INVARIANTS.md \
  tests/core/contract/test_extension_contract.py
git commit -m "test(memory): protect plugin host contracts"
```

## Completion Report Template

```text
完成内容：Assistant Memory Plugin API、排他 slot、Host 生命周期、受管多模态引用、默认 Mem0 Plugin、只读 CLI 与权威文档已完成。

Core invariant: EXT-001 changed because Memory Plugin 成为新的稳定 capability Plugin 扩展契约；DUR-001/OBS-001 行为保持不变并迁移到 Probe Memory Plugin Host。
Tests: added/updated tests/tdd/memory-plugin-api for temporary RED/GREEN; user may delete the directory manually. Updated existing core tests for EXT-001, DUR-001 and OBS-001.

验证：列出实际运行的 TDD、定向 core、裸 pytest、CLI smoke 和 git diff --check 命令与结果。
真实 Provider：未调用。
限制：v1 仅支持受信任进程内 Python Plugin；不提供 OpenClaw 兼容、跨进程沙箱、marketplace 或远端 Memory CRUD。
```
