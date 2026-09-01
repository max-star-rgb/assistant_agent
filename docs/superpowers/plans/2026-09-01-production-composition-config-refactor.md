# 生产装配与配置重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将扁平的 `ProviderConfig` 重构为按真实消费者组织的 `AppConfig`，集中环境装载并让 production composition 只向下游传递窄配置，同时保持全部运行行为不变。

**Architecture:** `load_app_config()` 是核心环境装载入口，返回由 `runtime/chat/vision/memory/media/tools` 组成的冻结 dataclass。`AgentServerExecutionOwner.compose()` 是唯一持有并向业务下游传递完整配置的 production composition root；`media_app` 只在 FastAPI 入口加载后立即投影。迁移先由旧加载器生成新模型，待全部消费者完成迁移后再把环境解析移入 `config/env.py` 并删除旧类，避免长期双轨和重复实现。

**Tech Stack:** Python、stdlib `dataclasses`、LangGraph/Deep Agents、pytest；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-09-01-production-composition-config-refactor-design.md`

## Global Constraints

- 保留全部现有环境变量、默认值、清洗规则、兼容回退、异常类型和主要错误信息。
- 保留 `MULTIMODAL_AGENT_PROVIDER_MODE=mock|real` 的显式安全边界；pytest 只运行 mock/offline。
- 不调用真实 Provider，不读取或写入真实凭据。
- 不新增全局配置容器、service locator、单实现 factory 或 `config/sections/**`。
- 不保留长期 `ProviderConfig` alias、扁平属性代理或新旧配置同步路径。
- 不移动 Tool、Media、Memory、Runtime 领域实现，不改变 Graph ID、Tool exposure、HITL、Memory lifecycle、Prompt 或视觉流水线。
- `mcp/config.py` 与 `observability/langsmith_config.py` 保持独立。
- 现有用户修改 `.run/Agent Server (Real).run.xml` 不属于本任务，禁止纳入提交。
- 临时测试只放 `tests/tdd/config-physical-refactor/`；用户可在功能完成后手动删除。
- 设计文档和本计划默认不提交；实施提交只包含当前任务源码、测试和 authority 变更。

---

## 文件结构与接口锁定

最终 `config` 目录只保留：

```text
src/assistant_agent/config/
├── __init__.py   # 薄导出
├── models.py     # 配置 dataclass 与纯数据校验
└── env.py        # 环境变量读取、清洗、兼容回退
```

新增公共接口：

```text
load_app_config(env: Mapping[str, str] | None = None) -> AppConfig
```

```python
@dataclass(frozen=True)
class AppConfig:
    provider_mode: ProviderMode = "mock"
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
```

字段必须逐项、原类型、原默认值迁入下列 owner；不得趁本切面删除字段：

| 配置段 | 迁入字段 |
| --- | --- |
| `AppConfig` | `provider_mode` |
| `RuntimeConfig` | `current_location`、`agent_service_text_turn_timeout_seconds`、`langgraph_checkpointer_backend`、`langgraph_checkpoint_path` |
| `ChatConfig` | `openai_api_key`、`qwen_api_key`、`dashscope_api_key`、`ark_api_key`、`chat_provider`、`chat_api_key`、`chat_base_url`、`chat_model`、`chat_adapter_kind`、`chat_stream`、`native_provider_streaming`、`chat_timeout_seconds`、`deep_research_chat_max_tokens`、全部 `context_*` 字段、`qwen_chat_enable_thinking`、`qwen_chat_enable_search`、`qwen_chat_api_protocol`、`openai_chat_*`、`qwen_chat_*`、`deepseek_*`、`ark_chat_*`、`local_chat_*` |
| `VisionConfig` | `qwen_vision_api_key`、`qwen_realtime_vision_api_key`、`ark_vision_api_key`、`seed_api_key`、`vision_provider`、`vision_api_key`、`vision_base_url`、`vision_model`、`vision_adapter_kind`、`vision_embedding_provider`、`embedding_provider`、全部 `vision_embedding_*`、`siglip2_*`、`embedding_cuda_device_id`、全部 `keyframe_*`、本地 `visual_memory_*`、全部 `visual_reminder_*`、`openai_vision_*`、`qwen_vision_*`、`qwen_realtime_vision_*`、`ark_vision_*`、`seed_vision_*`、全部 `visual_context_*` |
| `MemoryConfig` | 全部 `mem0_*`、`memory_backend`、`memory_commit_ledger_path`、`memory_extraction_delay_seconds`、`langmem_model`、全部 `conversation_history_*`、`max_conversation_history_turns`、全部 `editable_context_*` |
| `MediaConfig` | 全部 `proactive_*`、`remote_visual_memory_*`、`artifact_base_url`、`td_gen_ip`、`td_gen_port`、`public_ip`、`public_port`、`image_to_3d_timeout_seconds`、`video_understanding_timeout_seconds`、`max_video_bytes`、`max_video_seconds` |
| `ImageGenerationConfig` | `qwen_image_api_key`、`ark_image_api_key`、`comfyui_base_url`、全部 `image_generation_*`、`openai_image_model`、`qwen_image_*`、`ark_image_*`、`local_image_*` |
| `SearchConfig` | `search_api_base_url`、`search_provider`、全部 `web_search_*`、`tavily_api_key`、`tavily_base_url`、`visual_image_search_provider`、全部 `qwen_image_search_*` |
| `ShoppingConfig` | 全部 `shopping_search_*`、全部 `shopping_compare_*`、全部 `haodanku_*` |
| `LodgingConfig` | `lodging_provider`、`flyai_cli_path`、`flyai_api_key`、`flyai_timeout_seconds` |
| `ToolConfig` | `local_file_access_root`、`durable_tasks_enabled`，以及 `image_generation/search/shopping/lodging` 四个子配置 |

`ChatConfig`、`VisionConfig` 和 `ImageGenerationConfig` 各提供 `resolved_provider() -> ResolvedProviderSpec`；
方法只把该段的已解析通用字段委托给 `providers/specs.py` 的纯函数，不读取环境变量。`AppConfig` 保留
`has_any_real_provider() -> bool` 的纯数据能力。

下游接口统一为显式配置：

```text
create_chat_model(config: ChatConfig, *, provider_mode: ProviderMode) -> BaseChatModel
create_memory_backend(
    config: MemoryConfig,
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    media_config: MediaConfig,
    langmem_store: BaseStore | None = None,
) -> MemoryBackend
create_vision_adapter(
    config: VisionConfig,
    *,
    provider_mode: ProviderMode,
) -> VisionUnderstandingAdapter
create_native_tool_inventory(
    config: ToolConfig,
    *,
    provider_mode: ProviderMode,
    vision_config: VisionConfig,
    media_config: MediaConfig,
    resources: NativeToolResources,
    mcp_server_configs: Sequence[MCPServerConfig],
    mcp_client_factory: Callable[..., Any] | None = None,
    mcp_session_pool: ThreadMcpSessionPool | None = None,
) -> list[BaseTool]
```

---

### Task 1: 建立嵌套配置模型与临时等价性安全网

**Files:**
- Create: `tests/tdd/config-physical-refactor/test_app_config.py`
- Create: `src/assistant_agent/config/models.py`
- Create: `src/assistant_agent/config/env.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/providers/specs.py`

**Interfaces:**
- Consumes: 当前 `ProviderConfig.from_env(env)`。
- Produces: `AppConfig`、六个一级配置段、四个 Tool 子配置、`load_app_config(env)` 以及新旧配置等价性测试。

- [ ] **Step 1: 写入会因新接口不存在而失败的临时测试**

测试先导入尚不存在的 `AppConfig` 与 `load_app_config`，并定义只用于迁移的展开函数：

```python
from dataclasses import asdict

import pytest

from assistant_agent.config import AppConfig, ProviderConfig, load_app_config


def _flatten(config: AppConfig) -> dict[str, object]:
    values: dict[str, object] = {"provider_mode": config.provider_mode}
    for section_name in ("runtime", "chat", "vision", "memory", "media"):
        values.update(asdict(getattr(config, section_name)))
    values.update(
        {
            "local_file_access_root": config.tools.local_file_access_root,
            "durable_tasks_enabled": config.tools.durable_tasks_enabled,
        }
    )
    for section_name in ("image_generation", "search", "shopping", "lodging"):
        values.update(asdict(getattr(config.tools, section_name)))
    return values


@pytest.mark.parametrize(
    "env",
    [
        {},
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "qwen",
            "DASHSCOPE_API_KEY": "qwen-sentinel",
            "QWEN_CHAT_MODEL": "chat-sentinel",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "qwen",
            "MULTIMODAL_AGENT_IMAGE_PROVIDER": "qwen",
        },
        {
            "MULTIMODAL_AGENT_PROVIDER_MODE": "real",
            "MULTIMODAL_AGENT_CHAT_PROVIDER": "ark",
            "ARK_API_KEY": "ark-sentinel",
            "ARK_CHAT_MODEL": "ark-chat-sentinel",
            "MULTIMODAL_AGENT_VISION_PROVIDER": "ark",
            "ARK_IMAGE_MODEL": "ark-image-sentinel",
        },
    ],
)
def test_nested_config_matches_legacy_effective_values(
    env: dict[str, str],
) -> None:
    assert _flatten(load_app_config(env)) == asdict(ProviderConfig.from_env(env))
```

另加参数化失败等价测试，环境分别使用：

```python
INVALID_ENVIRONMENTS = [
    {"MULTIMODAL_AGENT_PROVIDER_MODE": "real"},
    {"MEMORY_BACKEND": "unknown"},
    {"REALTIME_KEYFRAME_SEMANTIC_THRESHOLD": "1.1"},
    {
        "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TARGET_RATIO": "0.8",
        "MULTIMODAL_AGENT_CONTEXT_COMPACTION_TRIGGER_RATIO": "0.7",
    },
    {"REALTIME_KEYFRAME_MIN_INTERVAL_SECONDS": "1"},
]
```

对每组环境分别捕获旧、新加载器的 `ValueError`，断言 `str(new_error.value) == str(old_error.value)`。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/config-physical-refactor/test_app_config.py
```

Expected: collection FAIL，原因是 `AppConfig` 或 `load_app_config` 尚未导出。

- [ ] **Step 3: 创建 Runtime 与 Chat 配置**

在 `models.py` 使用 `@dataclass(frozen=True)` 创建 `RuntimeConfig` 与 `ChatConfig`，按字段表机械迁移
原类型和默认值；把 context ratio 校验原文移动到 `ChatConfig.__post_init__`。

- [ ] **Step 4: 创建 Vision 配置**

创建 `VisionConfig`，迁入 Vision、Embedding、keyframe、本地视觉记忆、提醒和视觉上下文字段，并原样移动
CUDA device、阈值、Qdrant、result limit 和 visual context ratio 校验。

- [ ] **Step 5: 创建 Memory 与 Media 配置**

创建 `MemoryConfig` 与 `MediaConfig`。前者拥有 Mem0、LangMem、conversation/editable context；后者拥有
proactive delivery、remote visual memory、artifact/3D 和 video limits。各段只保留自己的正数、非空和
范围校验。

- [ ] **Step 6: 创建 Tool 子配置和 AppConfig**

创建 `ImageGenerationConfig`、`SearchConfig`、`ShoppingConfig`、`LodgingConfig`、`ToolConfig` 和
`AppConfig`，结构必须是：

```python
@dataclass(frozen=True)
class ToolConfig:
    local_file_access_root: str = ".data/files"
    durable_tasks_enabled: bool = False
    image_generation: ImageGenerationConfig = field(
        default_factory=ImageGenerationConfig
    )
    search: SearchConfig = field(default_factory=SearchConfig)
    shopping: ShoppingConfig = field(default_factory=ShoppingConfig)
    lodging: LodgingConfig = field(default_factory=LodgingConfig)


@dataclass(frozen=True)
class AppConfig:
    provider_mode: ProviderMode = "mock"
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)
```

把旧 `__post_init__` 校验按字段 owner 原样移动。只有“real 主 Chat 完整性”和“remote visual memory 要求
real + langmem”保留在 `AppConfig.__post_init__`；异常文本不得改写。

- [ ] **Step 7: 实现 Provider 解析委托**

在 `providers/specs.py` 增加三个接收已解析值的纯函数，避免配置模型重建环境映射：

```python
def resolved_chat_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> ResolvedProviderSpec:
    return resolved_provider_values(
        provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        specs=CHAT_PROVIDER_SPECS,
    )
```

同样实现 `resolved_vision_provider`、`resolved_image_generation_provider`，共同委托一个
`resolved_provider_values`。`ChatConfig.resolved_provider()`、`VisionConfig.resolved_provider()` 和
`ImageGenerationConfig.resolved_provider()` 只传自身 `provider/api_key/base_url/model`。

- [ ] **Step 8: 实现临时投影的 Runtime 与 Chat 部分**

`config/env.py` 的第一阶段实现仅调用旧加载器一次，然后构造嵌套模型：

```python
def load_app_config(
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    from assistant_agent.config import ProviderConfig

    legacy = ProviderConfig.from_env(env)
    return _app_config_from_legacy(legacy)
```

先让 `_app_config_from_legacy` 显式构造 `RuntimeConfig` 与 `ChatConfig`；不得用动态 `setattr`、
代理对象或把 `ProviderConfig` 保存进 `AppConfig`。

- [ ] **Step 9: 完成其余配置段投影**

继续显式构造 `VisionConfig`、`MemoryConfig`、`MediaConfig` 和 `ToolConfig` 的四个子段；用
Task 1 的 `_flatten` 等价测试确保 188 个字段全部且只归属一个 section。

- [ ] **Step 10: 将 `config/__init__.py` 收缩为新旧接口导出**

本阶段暂时继续导出旧类，并新增：

```python
from .env import load_app_config
from .models import (
    AppConfig,
    ChatConfig,
    ImageGenerationConfig,
    LodgingConfig,
    MediaConfig,
    MemoryConfig,
    RuntimeConfig,
    SearchConfig,
    ShoppingConfig,
    ToolConfig,
    VisionConfig,
)
```

- [ ] **Step 11: 运行等价性测试并修正所有字段遗漏**

运行 Task 1 命令。Expected: PASS，`_flatten(load_app_config(env))` 与旧 `asdict` 的 key 集和值完全相同。

- [ ] **Step 12: 增加默认对象的窄配置断言**

```python
def test_app_config_defaults_are_nested_and_mock_safe() -> None:
    config = AppConfig()
    assert config.provider_mode == "mock"
    assert config.chat.chat_provider == "mock"
    assert config.vision.vision_provider == "mock"
    assert config.tools.image_generation.image_generation_provider == "mock"
```

- [ ] **Step 13: 提交嵌套模型与安全网**

```bash
git add src/assistant_agent/config src/assistant_agent/providers/specs.py tests/tdd/config-physical-refactor
git commit -m "refactor: add nested application config"
```

---

### Task 2: 迁移 Chat、Context 与 Memory 消费者

**Files:**
- Modify: `src/assistant_agent/native_agent/providers.py`
- Modify: `src/assistant_agent/native_agent/memory.py`
- Modify: `src/assistant_agent/context/compactor.py`
- Modify: `src/assistant_agent/context/token_counter.py`
- Modify: `tests/tdd/config-physical-refactor/test_app_config.py`

**Interfaces:**
- Consumes: `ChatConfig`、`MemoryConfig`、`MediaConfig`、`ProviderMode`。
- Produces: 不读取环境变量的 Chat model、token counter、compactor 和 Memory backend factory。

- [ ] **Step 1: 写入显式依赖测试**

```python
def test_chat_and_memory_factories_accept_only_projected_config() -> None:
    config = load_app_config({})
    model = create_chat_model(config.chat, provider_mode=config.provider_mode)
    backend = create_memory_backend(
        config.memory,
        provider_mode=config.provider_mode,
        chat_config=config.chat,
        media_config=config.media,
        langmem_store=None,
    )
    assert model._llm_type == "assistant-agent-mock"
    assert backend is not None
```

- [ ] **Step 2: 运行该测试并确认 RED**

Expected: FAIL，现有 factory 仍要求 `ProviderConfig`。

- [ ] **Step 3: 修改 Chat model factory**

采用锁定签名：

```python
def create_chat_model(
    config: ChatConfig,
    *,
    provider_mode: ProviderMode,
) -> BaseChatModel:
    settings = config.resolved_provider()
```

删除函数内部 `ProviderConfig.from_env()` fallback；`_provider_extra_body` 改收 `ChatConfig`。

- [ ] **Step 4: 修改 token counter 与 compactor**

`create_context_token_counter` 只接收 `ChatConfig + provider_mode`；
`create_visual_context_token_counter` 接收 `ChatConfig + VisionConfig + provider_mode`。
`context/compactor.py` 只接收 `ChatConfig + provider_mode`。保持 tokenizer 路径、窗口和失败行为不变。

- [ ] **Step 5: 修改 Memory factory**

`create_memory_backend` 使用本 Task 锁定签名。`langmem_model` 覆盖通过
`replace(chat_config, chat_model=config.langmem_model)` 完成，再调用显式 `create_chat_model`；
remote visual memory 从 `media_config` 读取，所有现有校验和 retry 行为不变。

- [ ] **Step 6: 运行临时测试**

Run Task 1 命令。Expected: PASS。

- [ ] **Step 7: 运行受影响核心生命周期**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_context_lifecycle.py tests/core/integration/test_memory_lifecycle.py
```

Expected: PASS；若仅因 composition 尚未迁移而失败，在本 Task 内给调用点传入 `load_app_config()` 的对应段，
不得恢复叶子环境读取。

- [ ] **Step 8: 提交 Chat/Memory 迁移**

```bash
git add src/assistant_agent/native_agent/providers.py src/assistant_agent/native_agent/memory.py src/assistant_agent/context/compactor.py src/assistant_agent/context/token_counter.py tests/tdd/config-physical-refactor/test_app_config.py
git commit -m "refactor: narrow chat and memory config"
```

---

### Task 3: 迁移 Vision 与 Media 消费者

**Files:**
- Modify: `src/assistant_agent/providers/provider_selection.py`
- Modify: `src/assistant_agent/media/embedding/provider.py`
- Modify: `src/assistant_agent/media/video/detection/vision_embedding_provider.py`
- Modify: `src/assistant_agent/media/video/qdrant_visual_memory_index.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/media/video/video_adapter.py`
- Modify: `src/assistant_agent/media/video/visual_context_compactor.py`
- Modify: `src/assistant_agent/media/video/visual_timeline_compactor.py`
- Modify: `src/assistant_agent/media/vision/vision_client.py`
- Modify: `src/assistant_agent/media/visual_perception/module.py`
- Modify: `tests/tdd/config-physical-refactor/test_app_config.py`

**Interfaces:**
- Consumes: `VisionConfig`、`MediaConfig`、`ProviderMode`。
- Produces: 显式配置的 Vision、Embedding、Video 和 VisualPerception factories。

- [ ] **Step 1: 写入 mock Vision factory 测试**

```python
def test_vision_factories_use_projected_config() -> None:
    config = load_app_config({})
    adapter = create_vision_adapter(
        config.vision,
        provider_mode=config.provider_mode,
    )
    video = create_video_understanding_adapter(
        config.vision,
        provider_mode=config.provider_mode,
    )
    assert adapter.provider == "mock"
    assert video.provider == "mock"
```

- [ ] **Step 2: 运行并确认 RED**

Expected: FAIL，factory 仍接受或自行创建 `ProviderConfig`。

- [ ] **Step 3: 迁移 Provider/Vision/Embedding factory**

所有可选 `config=None` 签名改为必需 `VisionConfig`，并增加 keyword-only `provider_mode`。
删除 `ProviderConfig.from_env()` fallback。选中 Provider 使用 `config.resolved_provider()`；
embedding、SigLIP2、Qdrant 和 keyframe 参数从 `VisionConfig` 读取。

- [ ] **Step 4: 迁移视觉上下文和 Video adapter**

`visual_context_compactor.py` 与 `visual_timeline_compactor.py` 接收 `VisionConfig` 和显式
`provider_mode`；Video understanding factory 同样不读取环境变量。保持 timeout、最大视频限制和
mock adapter 结果不变。

- [ ] **Step 5: 收窄 `VisualPerceptionModule`**

构造器固定为：

```text
def __init__(
    self,
    *,
    provider_mode: ProviderMode,
    vision_config: VisionConfig,
    media_config: MediaConfig,
    data_root: Path | str = DEFAULT_VISUAL_PERCEPTION_ROOT,
    video_context_store: Any | None = None,
    realtime_video_memory_store: RealtimeVideoMemoryStore | None = None,
    visual_semantic_store_pool: SessionVisualSemanticStorePool | None = None,
    visual_memory_text_index: VisualMemoryTextIndex | None = None,
    observer_factory: ObserverFactory | None = None,
    vision_client: VisionUnderstandingClient | None = None,
    embedding_provider: MultimodalEmbeddingProvider | None = None,
) -> None
```

`get_visual_perception_module` 同样要求这三个显式参数；缓存和 close 语义不变。
`RealtimeVideoObserver` 接收 `VisionConfig + ProviderMode`，删除 `ProviderConfig()` fallback。

- [ ] **Step 6: 运行临时测试**

Run Task 1 命令。Expected: PASS。

- [ ] **Step 7: 运行现有视觉专项的离线 dry-run**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_system_realtime_visual_target_window_eval.py --dry-run
```

Expected: 输出 `status=dry_run` 语义且不读取图片、不调用 Provider。

- [ ] **Step 8: 提交 Vision/Media 迁移**

```bash
git add src/assistant_agent/providers/provider_selection.py src/assistant_agent/media tests/tdd/config-physical-refactor/test_app_config.py
git commit -m "refactor: narrow visual configuration"
```

---

### Task 4: 迁移 Tool inventory 与 Provider readiness

**Files:**
- Modify: `src/assistant_agent/tools/plugins/contracts.py`
- Modify: `src/assistant_agent/native_agent/tools.py`
- Modify: `src/assistant_agent/providers/provider_config_validation.py`
- Modify: `src/assistant_agent/providers/provider_readiness.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/visual_image_search/backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/visual_image_search/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/fetch_backend.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/local_file_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/plugin.py`
- Modify: `tests/tdd/config-physical-refactor/test_app_config.py`

**Interfaces:**
- Consumes: `ToolConfig`、`VisionConfig`、`MediaConfig`、`ProviderMode` 和现有 `NativeToolResources`。
- Produces: 不持有完整应用配置的 `ToolPluginContext` 与静态 Tool inventory。

- [ ] **Step 1: 写入 Tool inventory 显式配置测试**

```python
@pytest.mark.asyncio
async def test_tool_inventory_uses_projected_config() -> None:
    config = load_app_config({})
    tools = await create_native_tool_inventory(
        config.tools,
        provider_mode=config.provider_mode,
        vision_config=config.vision,
        media_config=config.media,
        resources=NativeToolResources(),
        mcp_server_configs=[],
    )
    assert tools
    assert len({tool.name for tool in tools}) == len(tools)
```

- [ ] **Step 2: 运行并确认 RED**

Expected: FAIL，当前 inventory 仍接受完整 `ProviderConfig`。

- [ ] **Step 3: 重塑 `ToolPluginContext`**

```python
@dataclass(frozen=True)
class ToolPluginContext:
    provider_mode: ProviderMode
    config: ToolConfig
    vision_config: VisionConfig
    media_config: MediaConfig
    # 其余 process resources 原样保留

    @property
    def mock_mode(self) -> bool:
        return self.provider_mode == "mock"
```

插件字段访问固定替换为：

- `context.config.image_generation`、`context.media_config.artifact_base_url`；
- `context.config.lodging`、`context.config.durable_tasks_enabled`；
- `context.config.local_file_access_root`；
- `context.vision_config.visual_memory_result_limit`。

- [ ] **Step 4: 迁移 builtin backend factory**

图片生成、购物、视觉图片搜索和 Web fetch backend 的可选完整配置参数改为各自子配置与
`provider_mode`；删除所有叶子 `load_app_config`/`ProviderConfig.from_env` fallback。

- [ ] **Step 5: 迁移 Provider validation/readiness**

签名固定为：

```text
validate_provider_config(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
) -> ProviderConfigValidationResult
build_provider_readiness_report(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
) -> ProviderReadinessReport
build_smoke_contract(
    *,
    provider_mode: ProviderMode,
    chat_config: ChatConfig,
    vision_config: VisionConfig,
    tool_config: ToolConfig,
    capability: str,
    provider: str,
    success: bool,
    errors: list[dict[str, object]] | None = None,
) -> ProviderSmokeContract
```

保留既有 Pydantic 输出 schema 和错误码；只改变输入形态。

- [ ] **Step 6: 运行临时测试和 Tool core contract**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/config-physical-refactor tests/core/contract/test_tool_contract.py tests/core/contract/test_extension_contract.py
```

Expected: PASS。

- [ ] **Step 7: 提交 Tool/Readiness 迁移**

```bash
git add src/assistant_agent/native_agent/tools.py src/assistant_agent/providers/provider_config_validation.py src/assistant_agent/providers/provider_readiness.py src/assistant_agent/tools/plugins tests/tdd/config-physical-refactor/test_app_config.py
git commit -m "refactor: project config into tool plugins"
```

---

### Task 5: 切换 production composition 和所有入口调用方

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `scripts/migrate_mem0_memories_to_chinese.py`
- Modify: `evals/system/common/preflight.py`
- Modify: `evals/system/realtime_visual_target_window/runner.py`
- Modify: `src/assistant_agent/multi_agent/a2a_adapter.py`
- Modify: `tests/core/support.py`
- Modify: `tests/core/contract/test_extension_contract.py`
- Modify: `tests/tdd/async-mcp-discovery/test_nonblocking_discovery.py`
- Modify: `tests/tdd/deep-agents-summarizer/test_main_model_output_limit.py`
- Modify: `tests/tdd/dashscope-native-visual-window/test_dashscope_native_visual_window.py`
- Modify: `tests/tdd/thread-artifact-stateful-mcp/test_stateful_mcp.py`

**Interfaces:**
- Consumes: Tasks 2–4 的所有窄 factory。
- Produces: 完整 `AppConfig` 只由 composition root 持有；入口和测试不再构造 `ProviderConfig`。

- [ ] **Step 1: 更新 `AgentServerExecutionOwner.compose()`**

```python
config = load_app_config()
context_token_counter = await asyncio.to_thread(
    create_context_token_counter,
    config.chat,
    provider_mode=config.provider_mode,
)
```

`_compose_sync(config: AppConfig, store)` 仍属于同一 composition 模块；它只把 `config.chat`、
`config.vision`、`config.memory`、`config.media`、`config.tools` 和 `config.provider_mode`
传给下游。`context_options` 从 `config.chat` 取值，`current_location` 从 `config.runtime` 取值。

- [ ] **Step 2: 更新 `media_app` 的入口投影**

FastAPI lifespan 只允许：

```python
loaded_config = load_app_config()
provider_mode = loaded_config.provider_mode
vision_config = loaded_config.vision
media_config = loaded_config.media
del loaded_config
```

用这些投影创建视觉模块、remote archive 和 proactive delivery。WebSocket handler 从
`application.state.visual_perception_module` 复用资源；不得在 handler 或叶子 factory 再次读取环境变量。

- [ ] **Step 3: 更新脚本和 eval 入口**

每个入口只调用一次 `load_app_config()`，随后传窄段：

- Mem0 中文迁移：`config.chat`、`config.memory`、`provider_mode`；
- system preflight：`config.chat`、`provider_mode`；
- realtime visual eval：`config.vision`、`config.media`、`provider_mode`。

保持真实调用的原有显式 operator 门禁；本任务只运行 dry-run。

- [ ] **Step 4: 更新测试构造**

`tests/core/support.py` 的 `offline_config()` 返回：

```python
return AppConfig(
    runtime=RuntimeConfig(langgraph_checkpointer_backend="none"),
)
```

Tool contract 测试向 inventory 传 `config.tools/vision/media/provider_mode`。
四个既有 TDD feature 使用 `AppConfig` 或 `load_app_config(env)`，不建立测试兼容类。
其中 `deep-agents-summarizer` 对 `chat_max_tokens` 的字段检查改查 `fields(ChatConfig)`，不改断言语义。

- [ ] **Step 5: 更新私密文本 marker**

`multi_agent/a2a_adapter.py` 中用于阻止内部实现泄露的 `"ProviderConfig"` marker 替换为
`"AppConfig"`；其他 marker 不变。

- [ ] **Step 6: 运行配置、入口和 core 定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/config-physical-refactor tests/core/contract/test_extension_contract.py tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_context_lifecycle.py tests/core/integration/test_memory_lifecycle.py
```

Expected: PASS。

- [ ] **Step 7: 提交 composition 切换**

```bash
git add src/assistant_agent/agent_server/services.py src/assistant_agent/agent_server/media_app.py src/assistant_agent/multi_agent/a2a_adapter.py scripts/migrate_mem0_memories_to_chinese.py evals/system/common/preflight.py evals/system/realtime_visual_target_window/runner.py tests/core/support.py tests/core/contract/test_extension_contract.py tests/tdd
git commit -m "refactor: centralize production app config"
```

---

### Task 6: 独立新环境加载器并删除旧类

**Files:**
- Modify: `src/assistant_agent/config/env.py`
- Modify: `src/assistant_agent/config/models.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/providers/specs.py`
- Modify: `tests/tdd/config-physical-refactor/test_app_config.py`
- Create: `tests/tdd/config-physical-refactor/expected_default_config.json`

**Interfaces:**
- Consumes: 已全部迁移到 `AppConfig` 的生产和非生产调用方。
- Produces: 独立 `load_app_config`、薄 `config/__init__.py`，仓库中不存在 `ProviderConfig`。

- [ ] **Step 1: 在删除旧类前固化默认快照**

使用旧 `ProviderConfig.from_env({})` 的 `asdict` 输出生成排序后的 JSON 内容，人工复核后通过
`apply_patch` 写入 `expected_default_config.json`。不得把真实 `os.environ` 或凭据写入快照。

- [ ] **Step 2: 增加新加载器独立性 RED 测试**

```python
def test_load_app_config_no_longer_delegates_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ProviderConfig,
        "from_env",
        classmethod(lambda cls, env=None: (_ for _ in ()).throw(
            AssertionError("legacy loader called")
        )),
    )
    assert load_app_config({}).provider_mode == "mock"
```

运行 Task 1 命令。Expected: FAIL with `legacy loader called`。

- [ ] **Step 3: 迁移共享环境解析 helper**

把旧 `from_env` 的完整逻辑和所有 `_bool_env/_int_env/_float_env`、兼容 alias、workspace URL、
provider selector helper 原样迁入 `env.py`。先实现 `_clean_env_source` 与 `_apply_provider_aliases`，
并保持 removed realtime keyframe 检查时机不变。

- [ ] **Step 4: 实现 Runtime 与 Chat 装载**

实现 `_load_runtime_config(source) -> RuntimeConfig` 和
`_load_chat_config(source, *, allow_real: bool) -> ChatConfig`；逐行移动旧 `from_env` 对应字段表达式。

- [ ] **Step 5: 实现 Vision 装载**

实现 `_load_vision_config(source, *, allow_real: bool) -> VisionConfig`，包括 embedding provider 兼容字段、
SigLIP2 model-dir alias、Qwen realtime workspace/region URL 和所有视觉数值解析。

- [ ] **Step 6: 实现 Memory 与 Media 装载**

实现 `_load_memory_config(source) -> MemoryConfig` 和 `_load_media_config(source) -> MediaConfig`；
remote visual memory 字段进入 Media，但 real/langmem 的跨段约束继续由 `AppConfig` 校验。

- [ ] **Step 7: 实现 Tool 装载**

实现 `_load_tool_config(source, *, allow_real: bool) -> ToolConfig`，在函数内构造
`ImageGenerationConfig`、`SearchConfig`、`ShoppingConfig` 和 `LodgingConfig`。

- [ ] **Step 8: 切换最终 `load_app_config`**

入口只协调六个按已确认 owner 划分的私有装载函数：

```python
def load_app_config(
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    source = _clean_env_source(os.environ if env is None else env)
    source = _apply_provider_aliases(source)
    provider_mode = get_provider_mode(source)
    allow_real = provider_mode == "real"
    return AppConfig(
        provider_mode=provider_mode,
        runtime=_load_runtime_config(source),
        chat=_load_chat_config(source, allow_real=allow_real),
        vision=_load_vision_config(source, allow_real=allow_real),
        memory=_load_memory_config(source),
        media=_load_media_config(source),
        tools=_load_tool_config(source, allow_real=allow_real),
    )
```

`_apply_provider_aliases` 只执行当前 `DASHSCOPE_API_KEY -> QWEN_API_KEY` 和 Qwen workspace URL
预处理；六个 `_load_*_config` 按“文件结构与接口锁定”的 188 字段 owner 表逐行接收旧 `from_env`
中的原表达式。不得改变表达式、默认值、执行顺序或异常文本，且 Task 1 等价性测试必须在删除旧类前通过。

- [ ] **Step 9: 运行新旧全量等价检查**

运行 Task 1 测试。Expected: 所有有效环境逐字段相等，所有非法环境异常文本相等。

- [ ] **Step 10: 把临时测试切换为最终契约测试**

移除测试对旧类的 import 和 monkeypatch，但不删除 TDD 目录。默认测试改为：

```python
def test_default_config_snapshot() -> None:
    expected = json.loads(
        Path(__file__).with_name("expected_default_config.json").read_text(
            encoding="utf-8"
        )
    )
    actual = json.loads(json.dumps(_flatten(load_app_config({})), sort_keys=True))
    assert actual == expected
```

保留 Qwen/DashScope alias、Ark、非法阈值和 removed realtime keyframe 的显式断言。

- [ ] **Step 11: 删除 `ProviderConfig` 并收缩包入口**

`config/__init__.py` 最终只 re-export `models.py` 类型和 `load_app_config`；旧类、旧
`from_env`、旧 helper 和临时 `_app_config_from_legacy` 全部删除。

- [ ] **Step 12: 检查残留引用**

```bash
rg -n '\bProviderConfig\b' src tests scripts evals --glob '*.py' | rg -v 'ProviderConfig(Issue|ValidationResult)'
```

Expected: 无输出。`ProviderConfigIssue`、`ProviderConfigValidationResult` 属 Provider readiness
结果类型，不在删除范围。

- [ ] **Step 13: 运行配置 TDD**

Run Task 1 命令。Expected: PASS。

- [ ] **Step 14: 提交加载器切换**

```bash
git add src/assistant_agent/config src/assistant_agent/providers/specs.py tests/tdd/config-physical-refactor
git commit -m "refactor: replace flat provider config"
```

---

### Task 7: 更新 authority 并完成离线验收

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Consumes: 最终 `AppConfig` 和 production composition。
- Produces: 与源码一致的当前 authority、通过的 core 与文档校验。

- [ ] **Step 1: 更新当前 authority**

准确记录：

- `AgentServerExecutionOwner.compose()` 持有完整 `AppConfig` 并向下游投影；
- `media_app` 只在 lifespan 入口加载并立即投影视觉/媒体配置；
- main/worker 共享同一 `ChatConfig` 的 context window、ratio 和启动期 token counter；
- Provider、Tool、Memory 和 Media 叶子模块不得读取环境变量。

不修改 `docs/authority.toml` 的 source globs，因为 `src/assistant_agent/config/**` 已被现有 domain 覆盖。

- [ ] **Step 2: 更新 CTX-001 的配置术语**

把 `tests/core/INVARIANTS.md` 中“共享 `ProviderConfig`”改为“共享 composition 投影的
`ChatConfig`”；不改变 CTX-001 的可观察行为或负责测试文件。

- [ ] **Step 3: 运行格式和编译检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent
git diff --check
```

Expected: 两个命令退出码均为 0。

- [ ] **Step 4: 运行临时 TDD 和完整 core**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/config-physical-refactor

MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: 全部 PASS，不访问网络或真实 Provider。

- [ ] **Step 5: 运行文档 authority 校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

Expected: 结构化结果成功，无缺失 owner、路由或 contract 错误。

- [ ] **Step 6: 检查最终物理边界**

```bash
test "$(wc -l < src/assistant_agent/config/__init__.py)" -lt 80
rg -n "from_env\(|load_app_config\(" src/assistant_agent/native_agent src/assistant_agent/providers src/assistant_agent/media src/assistant_agent/tools
```

Expected: `__init__.py` 小于 80 行；叶子目录没有配置环境读取。命中 Provider SDK 自身的 `from_env`
时逐项确认它不是应用配置加载。

- [ ] **Step 7: 验证现有 8089 hot reload**

先只检查现有 listener，不启动第二个服务：

```bash
ss -ltnp | rg ':8089\b'
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8089/ok
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8089/assistants/assistant-native-v4/graph >/dev/null
```

若 8089 未监听，记录“未执行 hot reload 验证”，不得自行启动并行 Server。若监听，检查
`/tmp/assistant_agent/logs/agent_server-8089.log` 的最新 reload 时间晚于源码修改，且日志没有配置装配异常。

- [ ] **Step 8: 提交 authority 与最终修正**

```bash
git add docs/agent-server-architecture.md docs/context_engineering_status.md docs/runtime-event-stream-architecture.md tests/core/INVARIANTS.md
git commit -m "docs: describe nested production config"
```

- [ ] **Step 9: 最终提交审计**

```bash
git status --short
git log --oneline --decorate -8
```

确认没有提交 `.run/Agent Server (Real).run.xml`、真实配置、缓存、生成物、设计文档或计划文档。
最终汇报必须包含：

```text
Core invariant: CTX-001 wording updated; observable behavior unchanged.
Tests: added tests/tdd/config-physical-refactor for temporary RED/GREEN; user may delete the directory manually.
Real Provider: not called.
```
