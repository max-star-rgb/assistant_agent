# 视觉 Tool 条件渐进暴露 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将上传媒体分析、实时画面读取和视觉历史搜索改造成三个基于可信运行事实独立渐进暴露的 Tool，并让上传媒体 Tool 复用进程级持久 VLM client。

**Architecture:** 所有 Tool 继续静态注册给 `create_agent` / `ToolNode`；新的 `ConditionalToolExposureMiddleware` 在每次 model call 前按封闭 availability 枚举过滤 `ModelRequest.tools`。上传媒体条件来自标准消息中的可信来源标记，VIDEO 条件来自 WebSocket 握手状态，视觉历史条件由窄 `VisualObservationHistoryProbe` 动态查询；三者不读取 Skill state。

**Tech Stack:** Python 3.11、LangChain `create_agent` / `AgentMiddleware` / `@tool`、LangGraph `ToolRuntime`、Pydantic v2、FastAPI WebSocket、pytest。

**Spec:** `docs/superpowers/specs/2026-08-18-conditional-visual-tool-exposure-design.md`

## Global Constraints

- `media_inspect` 重命名为 `uploaded_media_inspect`，生产环境不保留旧名称 alias。
- `uploaded_media_inspect` 同时支持用户主动上传的图片和视频，不读取摄像头实时视频。
- `live_view_inspect` 在当前 WebSocket 成功完成 `callType=VIDEO` 握手后暴露，不等待第一帧。
- `visual_memory_search` 仅在 VIDEO 握手完成且当前 user/session/as-of 已有可检索视觉文本时暴露。
- 三个 Tool 与 `active_skill_ids`、`skill_reference_grants`、`load_skill` 无关。
- Tool exposure 只读取结构化可信事实，不使用关键词、正则或额外 LLM 分类。
- 视觉观察历史是 session-scoped 派生时间线，不是 Agent 长期 Memory 或 LangGraph Store。
- 测试和验证固定使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- 不调整 `visual_reminder_manage` 的暴露策略。
- 用户批准的实现增量：三个视觉 Tool 均使用原生函数 Tool 工厂；复杂逻辑保留为普通 service，旧
  `ToolBase` 子类及视觉 Tool 之间的 Python 继承全部删除。
- 当前工作区已有大量未提交改动；每次提交前必须核对 staged diff，只提交本任务 hunks，禁止带入既有改动。

## File Structure

### 新建

- `src/assistant_agent/media/runtime_media.py`：从标准 HumanMessage content blocks 提取带来源的上传/实时媒体快照。
- `src/assistant_agent/tools/availability.py`：封闭的 Tool availability 标识和 metadata 读取函数。
- `src/assistant_agent/native_agent/conditional_tool_exposure.py`：统一条件裁决和 model-call Tool 过滤。
- `src/assistant_agent/media/visual_perception/history_probe.py`：视觉观察历史 availability 窄协议及 pool-backed 实现。
- `src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py`：上传媒体 inspection service 与原生函数 Tool 工厂。
- `src/assistant_agent/tools/plugins/builtin/media_inspection/live_view_tool.py`：与上传 Tool 解耦后的实时画面 Tool。
- `tests/tdd/conditional-visual-tool-exposure/test_runtime_media.py`：媒体来源与 VIDEO 握手 RED/GREEN。
- `tests/tdd/conditional-visual-tool-exposure/test_history_probe.py`：视觉历史 availability RED/GREEN。
- `tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py`：三种条件和 Skill 正交性 RED/GREEN。
- `tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py`：原生函数 Tool、名称和 VLM 复用 RED/GREEN。
- `evals/system/tools/uploaded_media_inspect.py`：重命名后的固定输入 smoke。

### 修改

- `src/assistant_agent/native_agent/context.py`：增加明确的 `realtime_media_mode` 运行事实。
- `src/assistant_agent/agent_server/media_session.py`：持有已绑定的 `call_type` 和 VIDEO 握手只读判断。
- `src/assistant_agent/agent_server/media_app.py`：投影 VIDEO 握手事实和 `source=live_camera` block。
- `src/assistant_agent/tools/base.py`：复用媒体快照、投影实时运行事实，并允许 ToolBase 声明 availability metadata。
- `src/assistant_agent/media/video/semantic_store.py`：提供 as-of-aware 的可检索历史判断。
- `src/assistant_agent/media/visual_perception/module.py`：进程级持有上传理解 client，暴露历史探针资源并统一关闭。
- `src/assistant_agent/tools/ids.py`：替换跨层 Tool 常量。
- `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`：装配原生上传 Tool、实时 Tool 和历史 Tool。
- `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`：声明条件并增加执行期保护。
- `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`：默认 Tool 名迁移，并只接受明确实时媒体引用。
- `src/assistant_agent/native_agent/tools.py`：携带视觉历史探针资源。
- `src/assistant_agent/native_agent/fast_agent.py`、`src/assistant_agent/agent_server/services.py`：接入条件 middleware。
- `skills/visual-context/SKILL.md`、`skills/visual-context/skill.toml`：更新 Tool 名；不把条件暴露改成 Skill 激活。
- `evals/system/tools/native_tool.py`、`evals/system/tools/_smoke_runner.py`：允许 smoke 显式传入可信 `AssistantRunContext`。
- `evals/system/tools/live_view_inspect.py`：更新实时 Tool import 和可信实时 block。
- `docs/tool-calling-architecture.md`、`docs/media-agent-service-websocket.md`、`docs/multimodal-embedding-architecture.md`：同步当前 authority。

### 删除

- `src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py`：旧 `MediaInspectTool` 与继承耦合完成迁移后删除。
- `evals/system/tools/media_inspect.py`：由 `uploaded_media_inspect.py` 替代。

---

### Task 1: 建立可信媒体来源与 VIDEO 握手事实

**Files:**
- Create: `src/assistant_agent/media/runtime_media.py`
- Modify: `src/assistant_agent/native_agent/context.py`
- Modify: `src/assistant_agent/agent_server/media_session.py`
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `src/assistant_agent/tools/base.py`
- Test: `tests/tdd/conditional-visual-tool-exposure/test_runtime_media.py`

**Interfaces:**
- Produces: `RuntimeMediaSnapshot`, `latest_runtime_media(state)`, `AssistantRunContext.realtime_media_mode`, `MediaConnectionSession.video_handshake_completed`。
- Consumes: 标准 `HumanMessage` content blocks；上传 block 必须为 `source="uploaded"`，实时 block 必须为 `source="live_camera"`。

- [ ] **Step 1: 写媒体来源与握手失败测试**

```python
from langchain_core.messages import HumanMessage

from assistant_agent.agent_server.media_app import media_graph_input
from assistant_agent.agent_server.media_session import MediaConnectionSession
from assistant_agent.media.runtime_media import latest_runtime_media


def test_runtime_media_separates_uploaded_and_live_video() -> None:
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "比较附件和当前画面"},
                    {"type": "image", "id": "image-upload", "source": "uploaded"},
                    {"type": "video", "id": "video-upload", "source": "uploaded"},
                    {
                        "type": "video",
                        "id": "video-live",
                        "source": "live_camera",
                        "target_sequence": 7,
                    },
                ]
            )
        ]
    }

    snapshot = latest_runtime_media(state)

    assert snapshot.uploaded_image_ids == ("image-upload",)
    assert snapshot.uploaded_video_ids == ("video-upload",)
    assert snapshot.live_video_ids == ("video-live",)
    assert snapshot.visual_target_sequence == 7


def test_video_handshake_fact_is_bound_by_control_not_first_frame() -> None:
    session = MediaConnectionSession(connection_id="connection-1")
    assert session.video_handshake_completed is False

    session.bind_control(
        protocol_session_id="vendor-session",
        user_id="user-1",
        thread_id="thread-1",
        call_type="VIDEO",
        media_capabilities=("audio", "video"),
    )

    assert session.video_ids == []
    assert session.video_handshake_completed is True


def test_media_graph_input_marks_camera_refs_as_live() -> None:
    chat = type("Chat", (), {"text": "现在看到了什么", "execution_mode": "fast"})()

    graph_input = media_graph_input(chat, video_ids=["video-live"])

    assert graph_input["messages"][0]["content"][1] == {
        "type": "video",
        "id": "video-live",
        "source": "live_camera",
    }
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_runtime_media.py
```

Expected: collection 或 assertion 失败，因为 `runtime_media`、`call_type` 和来源投影尚不存在。

- [ ] **Step 3: 实现不可伪造的媒体快照**

在 `runtime_media.py` 实现冻结值对象，只读取最新一条 `HumanMessage`：

```python
@dataclass(frozen=True)
class RuntimeMediaSnapshot:
    text: str = ""
    uploaded_image_ids: tuple[str, ...] = ()
    uploaded_video_ids: tuple[str, ...] = ()
    live_video_ids: tuple[str, ...] = ()
    visual_target_sequence: int | None = None

    @property
    def has_uploaded_media(self) -> bool:
        return bool(self.uploaded_image_ids or self.uploaded_video_ids)


def latest_runtime_media(state: Mapping[str, Any]) -> RuntimeMediaSnapshot:
    """Project only explicitly sourced media from the latest HumanMessage."""
```

规则固定为：`image|image_url + source=uploaded` 进入 uploaded images；`video|file + source=uploaded` 进入 uploaded videos；`video + source=live_camera` 进入 live videos；无来源 block 不进入三组媒体引用。文本仍正常合并。

- [ ] **Step 4: 投影握手状态和运行 Context**

在 `AssistantRunContext` 增加：

```python
realtime_media_mode: Literal["none", "video"] = "none"
```

在 `MediaConnectionSession` 增加 `call_type: Literal["AUDIO", "VIDEO"] | None`，让 `bind_control(protocol_session_id=frame.session_id, user_id=user_id, thread_id=thread_id, call_type=call_type, client_capabilities=_client_capabilities(frame.body), media_capabilities=(("audio", "video") if call_type == "VIDEO" else ("audio",)))` 一次性绑定，并实现：

```python
@property
def video_handshake_completed(self) -> bool:
    return self.thread_id is not None and self.call_type == "VIDEO"
```

`_run_chat()` 的 context 使用：

```python
"realtime_media_mode": (
    "video" if session.video_handshake_completed else "none"
),
```

`media_graph_input()` 给摄像头 video block 增加 `source="live_camera"`。`tools.base` 的旧 request 提取改为委托 `latest_runtime_media()`，并分别提供 `uploaded_image_ids`、`uploaded_video_ids` 和 `live_video_ids`，不再把两种 video 混在一起。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run: 与 Step 2 相同。

Expected: 全部 PASS。

- [ ] **Step 6: 提交 Task 1**

```bash
git add \
  src/assistant_agent/media/runtime_media.py \
  src/assistant_agent/native_agent/context.py \
  src/assistant_agent/agent_server/media_session.py \
  src/assistant_agent/agent_server/media_app.py \
  src/assistant_agent/tools/base.py \
  tests/tdd/conditional-visual-tool-exposure/test_runtime_media.py
git diff --cached --check
git commit -m "feat: add trusted runtime media facts"
```

### Task 2: 提供窄视觉历史 availability 探针

**Files:**
- Create: `src/assistant_agent/media/visual_perception/history_probe.py`
- Modify: `src/assistant_agent/media/video/semantic_store.py`
- Modify: `src/assistant_agent/media/visual_perception/module.py`
- Modify: `src/assistant_agent/media/visual_perception/__init__.py`
- Test: `tests/tdd/conditional-visual-tool-exposure/test_history_probe.py`

**Interfaces:**
- Consumes: `SessionVisualSemanticStorePool.peek(user_id, session_id)` 和可信 `as_of_sequence`。
- Produces: `VisualObservationHistoryProbe.has_searchable_observations(*, user_id, session_id, as_of_sequence) -> bool`。

- [ ] **Step 1: 写探针 RED 测试**

```python
from types import SimpleNamespace

import pytest

from assistant_agent.media.video.semantic_store import VisualSemanticRecord
from assistant_agent.media.video.semantic_store_pool import (
    SessionVisualSemanticStorePool,
)
from assistant_agent.media.visual_perception.history_probe import (
    PoolVisualObservationHistoryProbe,
)


@pytest.fixture
def semantic_pool_with_records(tmp_path):
    pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic")
    store = pool.resolve("user-1", "session-1")
    for sequence, index_status in ((6, "unavailable"), (7, "ready")):
        evidence = tmp_path / f"frame-{sequence}.jpg"
        evidence.write_bytes(f"frame-{sequence}".encode())
        searchable = index_status == "ready"
        store.record_success(
            VisualSemanticRecord(
                record_id=f"record-{sequence}",
                session_id="session-1",
                video_id="video-1",
                frame_sequence=sequence,
                summary=f"observation-{sequence}",
                index_status=index_status,
                search_embedding=([1.0, 0.0] if searchable else None),
                embedding_space_id=("space-1" if searchable else None),
                evidence_ref=str(evidence),
                evidence_bytes=evidence.stat().st_size,
                created_at_ms=sequence * 1000,
            )
        )
    try:
        yield SimpleNamespace(pool=pool)
    finally:
        pool.close()


@pytest.fixture
def empty_semantic_pool(tmp_path):
    pool = SessionVisualSemanticStorePool(root=tmp_path / "empty-semantic")
    try:
        yield pool
    finally:
        pool.close()


def test_history_probe_requires_searchable_record_at_as_of_boundary(
    semantic_pool_with_records,
) -> None:
    probe = PoolVisualObservationHistoryProbe(semantic_pool_with_records.pool)

    assert probe.has_searchable_observations(
        user_id="user-1", session_id="session-1", as_of_sequence=6
    ) is False
    assert probe.has_searchable_observations(
        user_id="user-1", session_id="session-1", as_of_sequence=7
    ) is True


def test_history_probe_does_not_create_missing_session(empty_semantic_pool) -> None:
    probe = PoolVisualObservationHistoryProbe(empty_semantic_pool)

    assert probe.has_searchable_observations(
        user_id="user-1", session_id="missing", as_of_sequence=None
    ) is False
    assert empty_semantic_pool.peek("user-1", "missing") is None
```

测试 fixture 写入两条 `VisualSemanticRecord`：sequence 6 的 `index_status="unavailable"` 和 sequence 7 的 `index_status="ready"` + 非空 `search_embedding` + `embedding_space_id`；evidence 使用 `tmp_path` 下的小文件。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_history_probe.py
```

Expected: FAIL，因为 history probe 和 as-of-aware 判断尚不存在。

- [ ] **Step 3: 扩展 semantic store 的结构化判断**

将现有方法改为：

```python
def has_searchable_history(self, *, as_of_sequence: int | None = None) -> bool:
    if as_of_sequence is not None and as_of_sequence < 0:
        raise ValueError("visual semantic as-of sequence must be non-negative")
    with self._lock:
        self._ensure_open()
        return any(
            (as_of_sequence is None or record.frame_sequence <= as_of_sequence)
            and record.index_status == "ready"
            and record.search_embedding is not None
            for record in self._records.values()
        )
```

- [ ] **Step 4: 实现窄 pool-backed probe**

```python
class VisualObservationHistoryProbe(Protocol):
    def has_searchable_observations(
        self,
        *,
        user_id: str,
        session_id: str,
        as_of_sequence: int | None,
    ) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class PoolVisualObservationHistoryProbe:
    pool: SessionVisualSemanticStorePool

    def has_searchable_observations(
        self,
        *,
        user_id: str,
        session_id: str,
        as_of_sequence: int | None,
    ) -> bool:
        store = self.pool.peek(user_id, session_id)
        return bool(
            store is not None
            and store.has_searchable_history(as_of_sequence=as_of_sequence)
        )
```

`VisualPerceptionModule` 创建一次 `PoolVisualObservationHistoryProbe(self.visual_semantic_store_pool)`；`VisualPerceptionToolResources` 增加 `visual_history_probe: VisualObservationHistoryProbe`，`tool_resources()` 返回该实例。`media.visual_perception.__init__` 导出 probe protocol 和实现；middleware 不直接接触 pool、Qdrant 或 SQLite。

- [ ] **Step 5: 运行测试并确认 GREEN**

Run: 与 Step 2 相同。

Expected: 全部 PASS。

- [ ] **Step 6: 提交 Task 2**

```bash
git add \
  src/assistant_agent/media/visual_perception/history_probe.py \
  src/assistant_agent/media/video/semantic_store.py \
  src/assistant_agent/media/visual_perception/module.py \
  src/assistant_agent/media/visual_perception/__init__.py \
  tests/tdd/conditional-visual-tool-exposure/test_history_probe.py
git diff --cached --check
git commit -m "feat: expose visual history availability probe"
```

### Task 3: 实现独立于 Skill 的中央条件暴露 middleware

**Files:**
- Create: `src/assistant_agent/tools/availability.py`
- Create: `src/assistant_agent/native_agent/conditional_tool_exposure.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Test: `tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py`

**Interfaces:**
- Consumes: Task 1 `latest_runtime_media()`、`AssistantRunContext.realtime_media_mode`；Task 2 `VisualObservationHistoryProbe`。
- Produces: `ToolAvailability`、`ConditionalToolExposureMiddleware(history_probe=None)`；只过滤传入 `request.tools`。

- [ ] **Step 1: 写三条件和 Skill 正交性 RED 测试**

```python
from types import SimpleNamespace

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from assistant_agent.native_agent.conditional_tool_exposure import (
    ConditionalToolExposureMiddleware,
)
from assistant_agent.native_agent.context import AssistantRunContext
from assistant_agent.native_agent.providers import MockAssistantChatModel


class FakeHistoryProbe:
    def __init__(self, available: bool = False) -> None:
        self.available = available

    def has_searchable_observations(
        self,
        *,
        user_id: str,
        session_id: str,
        as_of_sequence: int | None,
    ) -> bool:
        assert user_id == "user-1"
        assert session_id == "thread-1"
        return self.available


def model_request_runtime(
    *,
    context: AssistantRunContext,
    user_id: str = "user-1",
    thread_id: str = "thread-1",
):
    return SimpleNamespace(
        context=context,
        server_info=SimpleNamespace(user=SimpleNamespace(identity=user_id)),
        execution_info=SimpleNamespace(thread_id=thread_id, run_id="run-1"),
    )


def _visible_names(middleware, tools, messages, runtime) -> list[str]:
    observed: list[str] = []

    def handler(request: ModelRequest) -> AIMessage:
        observed.extend(tool.name for tool in request.tools)
        return AIMessage(content="done")

    middleware.wrap_model_call(
        ModelRequest(
            model=MockAssistantChatModel(),
            messages=messages,
            tools=tools,
            state={"messages": messages},
            runtime=runtime,
        ),
        handler,
    )
    return observed


def _conditional_tool(name: str, availability: str) -> StructuredTool:
    def probe(query: str = "sentinel") -> str:
        return query
    return StructuredTool.from_function(
        probe,
        name=name,
        metadata={"effect": "read", "availability": availability},
    )


def test_uploaded_tool_requires_explicit_uploaded_block() -> None:
    middleware = ConditionalToolExposureMiddleware()
    tools = [_conditional_tool("uploaded_media_inspect", "uploaded_media_present")]

    hidden = _visible_names(
        middleware,
        tools,
        [HumanMessage(content=[{"type": "video", "id": "live", "source": "live_camera"}])],
        model_request_runtime(context=AssistantRunContext()),
    )
    visible = _visible_names(
        middleware,
        tools,
        [HumanMessage(content=[{"type": "video", "id": "upload", "source": "uploaded"}])],
        model_request_runtime(context=AssistantRunContext()),
    )

    assert hidden == []
    assert visible == ["uploaded_media_inspect"]


def test_live_and_history_conditions_progress_independently() -> None:
    fake_history_probe = FakeHistoryProbe()
    middleware = ConditionalToolExposureMiddleware(fake_history_probe)
    tools = [
        _conditional_tool("live_view_inspect", "video_handshake_completed"),
        _conditional_tool("visual_memory_search", "visual_history_available"),
    ]
    runtime = model_request_runtime(
        context=AssistantRunContext(realtime_media_mode="video"),
        user_id="user-1",
        thread_id="thread-1",
    )

    fake_history_probe.available = False
    assert _visible_names(middleware, tools, [HumanMessage("q")], runtime) == [
        "live_view_inspect"
    ]

    fake_history_probe.available = True
    assert _visible_names(middleware, tools, [HumanMessage("q")], runtime) == [
        "live_view_inspect",
        "visual_memory_search",
    ]


def test_condition_filter_never_readds_tools_removed_by_skill_layer() -> None:
    general_tool = _conditional_tool("general_probe", "always")
    observed_names: list[str] = []

    def handler(request: ModelRequest) -> AIMessage:
        observed_names.extend(tool.name for tool in request.tools)
        return AIMessage(content="done")

    request = ModelRequest(
        model=MockAssistantChatModel(),
        messages=[HumanMessage(content="sentinel")],
        tools=[general_tool],
        state={"messages": [HumanMessage(content="sentinel")]},
        runtime=model_request_runtime(context=AssistantRunContext()),
    )
    ConditionalToolExposureMiddleware().wrap_model_call(request, handler)
    assert observed_names == ["general_probe"]
```

`_visible_names` 在测试内构造 `ModelRequest`，handler 记录 `request.tools` 并返回 `AIMessage(content="done")`；fake runtime 提供 `context`、`server_info.user.identity` 和 `execution_info.thread_id`。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py
```

Expected: collection 失败，因为 availability contract 和 middleware 尚不存在。

- [ ] **Step 3: 实现封闭 availability contract**

```python
class ToolAvailability(str, Enum):
    ALWAYS = "always"
    UPLOADED_MEDIA_PRESENT = "uploaded_media_present"
    VIDEO_HANDSHAKE_COMPLETED = "video_handshake_completed"
    VISUAL_HISTORY_AVAILABLE = "visual_history_available"


def tool_availability(tool: BaseTool) -> ToolAvailability:
    raw = (tool.metadata or {}).get("availability", ToolAvailability.ALWAYS.value)
    return ToolAvailability(raw)
```

未知 availability 必须在装配或 middleware 初始化时抛 `ValueError`，不能默认为可见。`ToolBase` 增加 `availability: ClassVar[ToolAvailability] = ToolAvailability.ALWAYS`，并把枚举值写入标准 metadata。

- [ ] **Step 4: 实现同步与异步 model-call 过滤**

```python
class ConditionalToolExposureMiddleware(AgentMiddleware):
    def __init__(self, history_probe: VisualObservationHistoryProbe | None = None):
        self._history_probe = history_probe

    def wrap_model_call(self, request, handler):
        return handler(self._request_with_visible_tools(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request_with_visible_tools(request))
```

裁决规则：

- `ALWAYS`：保留；
- `UPLOADED_MEDIA_PRESENT`：`latest_runtime_media(request.state).has_uploaded_media`；
- `VIDEO_HANDSHAKE_COMPLETED`：`request.runtime.context.realtime_media_mode == "video"`；
- `VISUAL_HISTORY_AVAILABLE`：先要求 VIDEO mode，再读取 authenticated user、execution thread 和最新实时 block 的 `target_sequence`，调用 probe；任何缺失或 probe 异常都 fail closed。

只遍历 `request.tools`，不得从构造时 inventory 重新添加 Tool。

- [ ] **Step 5: 接入 shared fast agent**

`build_fast_agent()` 增加：

```python
visual_history_probe: VisualObservationHistoryProbe | None = None
```

middleware 顺序保持 prompt 后先做 Skill 过滤，再做条件过滤：

```python
middleware = [
    assistant_prompt,
    ProgressiveToolExposureMiddleware(resolved_skill_catalog),
    ConditionalToolExposureMiddleware(visual_history_probe),
    ModelCallLimitMiddleware(run_limit=model_call_limit, exit_behavior="error"),
    ToolCallLimitMiddleware(run_limit=tool_call_limit, exit_behavior="error"),
]
```

两个过滤器都只缩小当前 `request.tools`，最终自然得到交集。

- [ ] **Step 6: 运行条件测试和现有 Skill TDD**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py \
  tests/tdd/progressive-native-tool-exposure/test_progressive_tool_exposure.py
```

Expected: 全部 PASS，Skill state 不改变三个视觉条件。

- [ ] **Step 7: 提交 Task 3**

```bash
git add \
  src/assistant_agent/tools/availability.py \
  src/assistant_agent/native_agent/conditional_tool_exposure.py \
  src/assistant_agent/tools/base.py \
  src/assistant_agent/native_agent/fast_agent.py \
  tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py
git diff --cached --check
git commit -m "feat: add conditional tool exposure middleware"
```

### Task 4: 将上传理解 VLM client 改为进程级持久资源

**Files:**
- Modify: `src/assistant_agent/media/visual_perception/module.py`
- Test: `tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py`

**Interfaces:**
- Consumes: `create_vision_understanding_client(config)`。
- Produces: `VisualPerceptionModule.understand(request)` 复用同一 `_vision_client`；`aclose()` 只关闭一次。

- [ ] **Step 1: 写持久 client RED 测试**

```python
class RecordingVisionClient:
    def __init__(self) -> None:
        self.requests: list[VisionUnderstandingRequest] = []
        self.close_count = 0

    def understand(
        self, request: VisionUnderstandingRequest
    ) -> VisionUnderstandingResult:
        self.requests.append(request)
        return VisionUnderstandingResult(
            summary="media-sentinel",
            provider="mock",
            output_ref="vision:sentinel",
        )

    def close(self) -> None:
        self.close_count += 1


def test_visual_module_reuses_uploaded_media_client_and_closes_once(
    monkeypatch, tmp_path
) -> None:
    client = RecordingVisionClient()
    create_count = 0

    def create_client(_config):
        nonlocal create_count
        create_count += 1
        return client

    monkeypatch.setattr(module, "create_vision_understanding_client", create_client)
    visual = module.VisualPerceptionModule(data_root=tmp_path)
    request = VisionUnderstandingRequest(
        image_ids=["image-1"], question="有什么"
    )

    visual.understand(request)
    visual.understand(request)
    asyncio.run(visual.aclose())
    asyncio.run(visual.aclose())

    assert create_count == 1
    assert len(client.requests) == 2
    assert client.close_count == 1
```

`RecordingVisionClient.understand()` 返回固定 `VisionUnderstandingResult`，`close()` 递增 `close_count`。

- [ ] **Step 2: 运行单测并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py \
  -k reuses_uploaded_media_client
```

Expected: FAIL，当前 `understand()` 每次创建并关闭 client。

- [ ] **Step 3: 将 client 创建和关闭移到模块生命周期**

构造器增加可测试注入点：

```python
vision_client: VisionUnderstandingClient | None = None
```

初始化：

```python
self._vision_client = vision_client or create_vision_understanding_client(self.config)
self._vision_client_lock = Lock()
```

调用：

```python
def understand(self, request: VisionUnderstandingRequest) -> VisionUnderstandingResult:
    if self._closed:
        raise RuntimeError("visual_perception_module_closed")
    with self._vision_client_lock:
        return self._vision_client.understand(request)
```

`aclose()` 在所有 realtime sessions 关闭后调用 `_vision_client.close()` 一次；保持幂等。`tool_resources().vision_client` 继续暴露模块边界或同一持久 client，不允许 Plugin 再创建备用 client。

- [ ] **Step 4: 运行持久 client 测试和现有实时观察生命周期测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py \
  tests/tdd/realtime-visual-observation-service/test_observation_service.py \
  -k 'client or lifespan or session'
```

Expected: 全部 PASS；实时 session 仍独立关闭自己的 observer client。

- [ ] **Step 5: 提交 Task 4**

```bash
git add \
  src/assistant_agent/media/visual_perception/module.py \
  tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py
git diff --cached --check
git commit -m "refactor: persist uploaded media vision client"
```

### Task 5: 重命名并重构为原生上传媒体函数 Tool

**Files:**
- Create: `src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py`
- Create: `src/assistant_agent/tools/plugins/builtin/media_inspection/live_view_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/tools/ids.py`
- Modify: `src/assistant_agent/tools/base.py`
- Delete: `src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py`
- Test: `tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py`

**Interfaces:**
- Consumes: Task 1 `latest_runtime_media()`、Task 3 availability metadata、Task 4 持久 `VisionUnderstandingClient`。
- Produces: `create_uploaded_media_inspect_tool(client, *, video_adapter=None, context_store=None) -> BaseTool`、独立 `LiveViewInspectTool`、`UPLOADED_MEDIA_INSPECT_TOOL_NAME`。

- [ ] **Step 1: 写名称、schema、来源和 client 调用 RED 测试**

```python
def native_tool_runtime(*, content, context=None) -> ToolRuntime:
    return ToolRuntime(
        state={"messages": [HumanMessage(content=content)]},
        context=context or AssistantRunContext(),
        config={},
        stream_writer=lambda chunk: None,
        tool_call_id="tool-call-1",
        store=None,
        execution_info=SimpleNamespace(thread_id="thread-1", run_id="run-1"),
        server_info=SimpleNamespace(
            user=SimpleNamespace(identity="user-1")
        ),
    )


def test_uploaded_media_tool_is_native_and_only_exposes_question() -> None:
    tool = create_uploaded_media_inspect_tool(RecordingVisionClient())

    assert tool.name == "uploaded_media_inspect"
    assert set(tool.tool_call_schema.model_fields) == {"question"}
    assert tool.metadata["availability"] == "uploaded_media_present"


def test_uploaded_media_tool_passes_only_uploaded_refs_to_vlm() -> None:
    recording_client = RecordingVisionClient()
    tool = create_uploaded_media_inspect_tool(recording_client)
    runtime = native_tool_runtime(
        content=[
            {"type": "text", "text": "附件里有什么"},
            {"type": "image", "id": "image-upload", "source": "uploaded"},
            {"type": "video", "id": "video-upload", "source": "uploaded"},
            {"type": "video", "id": "video-live", "source": "live_camera"},
        ]
    )

    content, artifact = tool._run(question="重点看标签", runtime=runtime)

    [request] = recording_client.requests
    assert request.image_ids == ["image-upload"]
    assert request.video_ids == ["video-upload"]
    assert "video-live" not in request.video_ids
    assert request.question == "重点看标签"
    assert json.loads(content)["summary"] == "media-sentinel"
    assert artifact["summary"] == "media-sentinel"


def test_uploaded_media_tool_rejects_missing_uploaded_attachment() -> None:
    recording_client = RecordingVisionClient()
    tool = create_uploaded_media_inspect_tool(recording_client)
    runtime = native_tool_runtime(
        content=[{"type": "video", "id": "video-live", "source": "live_camera"}]
    )

    with pytest.raises(ToolException, match="uploaded_media_required"):
        tool._run(question="看附件", runtime=runtime)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py \
  -k 'native or uploaded_refs or missing_uploaded'
```

Expected: FAIL，因为新常量、工厂和来源保护尚不存在。

- [ ] **Step 3: 提取上传媒体 inspection service**

`uploaded_tool.py` 定义：

```python
class UploadedMediaInspector:
    def __init__(
        self,
        *,
        client: VisionUnderstandingClient,
        video_branch: VideoUnderstandingBranch,
    ) -> None:
        self.client = client
        self.video_branch = video_branch

    def inspect(
        self,
        request: VisionUnderstandingRequest,
        context: ToolContext,
    ) -> ToolResult:
        if vision_request_has_video(request):
            result = self.video_branch.execute(
                video_request_from_vision_request(request), context
            )
            return result.model_copy(
                update={"tool_name": UPLOADED_MEDIA_INSPECT_TOOL_NAME}
            )
        return self._inspect_images(request, context)
```

把旧 `MediaInspectTool._execute()` 的图片 observation、Provider error 和 explicit-video branch 迁入该 service；explicit video 的 `VideoUnderstandingBranch(tool_name="uploaded_media_inspect")` 不得进入 Agent-Service live text-only 分支。通过 request metadata 明确 `media_source="uploaded"`，`VideoUnderstandingBranch` 只把 `source=live_camera` +实时 Tool 上下文解释为 live。

- [ ] **Step 4: 用原生函数工厂封装 service**

```python
def create_uploaded_media_inspect_tool(
    client: VisionUnderstandingClient,
    *,
    video_adapter: VideoUnderstandingAdapter | None = None,
    context_store: VideoContextStore | None = None,
) -> BaseTool:
    inspector = UploadedMediaInspector(
        client=client,
        video_branch=VideoUnderstandingBranch(
            tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME,
            client=client,
            adapter=video_adapter,
            context_store=context_store,
        ),
    )

    @tool(
        UPLOADED_MEDIA_INSPECT_TOOL_NAME,
        response_format="content_and_artifact",
        metadata={
            "effect": "read",
            "source": "builtin",
            "availability": ToolAvailability.UPLOADED_MEDIA_PRESENT.value,
        },
    )
    def uploaded_media_inspect(
        question: Annotated[str, Field(min_length=1, max_length=500)],
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[str, dict[str, Any]]:
        snapshot = latest_runtime_media(runtime.state)
        if not snapshot.has_uploaded_media:
            raise ToolException(
                "uploaded_media_required: 当前请求没有用户主动上传的图片或视频"
            )
        execution = runtime.execution_info
        request = VisionUnderstandingRequest(
            image_ids=list(snapshot.uploaded_image_ids),
            video_ids=list(snapshot.uploaded_video_ids),
            question=question,
            user_query=snapshot.text,
            user_id=authenticated_user_identity(runtime),
            session_id=getattr(execution, "thread_id", None),
            metadata={"media_source": "uploaded"},
        )
        context = tool_context_from_runtime(runtime)
        return tool_result_content_and_artifact(
            inspector.inspect(request, context),
            tool_name=UPLOADED_MEDIA_INSPECT_TOOL_NAME,
        )

    return uploaded_media_inspect
```

`tool_context_from_runtime()` 和 `tool_result_content_and_artifact()` 从 `tools.base` 提取为可复用公共 helper；前者只读取可信 Runtime，后者复用现有成功 observation/artifact 与失败脱敏 `ToolException` 规则。没有上传引用时使用固定错误码 `uploaded_media_required`。

- [ ] **Step 5: 解除 LiveView 对旧类的继承**

在 `live_view_tool.py` 创建独立 `LiveViewInspectTool(ToolBase)`：

- `availability = ToolAvailability.VIDEO_HANDSHAKE_COMPLETED`；
- runtime bindings 使用 `live_video_ids`，不使用所有 `video_ids`；
- `_execute()` 先要求 `context.metadata["realtime_media_mode"] == "video"`，否则返回失败 `ToolResult(error="video_handshake_required")`；
- 把 `query` 映射到 `VisionUnderstandingRequest.user_query` 后直接调用自己的 `VideoUnderstandingBranch(tool_name="live_view_inspect", client=client, context_store=context_store, memory_store=memory_store, semantic_store_pool=semantic_store_pool)`。

删除旧 `MediaInspectTool` 和继承关系。将常量改为：

```python
UPLOADED_MEDIA_INSPECT_TOOL_NAME = "uploaded_media_inspect"
```

删除 `MEDIA_INSPECT_TOOL_NAME` 和 `IMAGE_UNDERSTANDING_TOOL_NAME` 兼容 alias。

- [ ] **Step 6: 更新 Plugin 装配并禁止备用 client**

`MediaInspectionPlugin.build_tools()` 在 `vision_ready` 时必须要求 composition 注入 `context.vision_client`；构造：

```python
create_uploaded_media_inspect_tool(
    context.vision_client,
    context_store=context.video_context_store,
)
LiveViewInspectTool(
    client=context.vision_client,
    context_store=context.video_context_store,
    memory_store=context.realtime_video_memory_store,
    semantic_store_pool=context.visual_semantic_store_pool,
)
```

移除 Plugin 内 `create_vision_understanding_client()` 的逐 Tool fallback，避免出现第二个未纳入模块生命周期的 client。

- [ ] **Step 7: 运行上传、实时和 Plugin 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py \
  tests/tdd/realtime-visual-observation-service/test_observation_service.py
```

Expected: 全部 PASS；Tool inventory 只出现新名称。

- [ ] **Step 8: 提交 Task 5**

```bash
git add \
  src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/live_view_tool.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py \
  src/assistant_agent/tools/ids.py \
  src/assistant_agent/tools/base.py \
  tests/tdd/conditional-visual-tool-exposure/test_uploaded_media_tool.py
git diff --cached --check
git commit -m "refactor: add native uploaded media inspect tool"
```

### Task 6: 接通视觉历史条件、生产资源与执行期保护

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/native_agent/tools.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/native_agent/fast_agent.py`
- Modify: `tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py`

**Interfaces:**
- Consumes: Task 2 probe、Task 3 middleware、Task 5 Tool metadata。
- Produces: production `create_agent` 的最终条件过滤，以及 `visual_memory_search` 的执行期 VIDEO 保护。

- [ ] **Step 1: 写 production composition RED 测试**

```python
def test_fast_agent_receives_visual_history_probe(monkeypatch) -> None:
    probe = FakeHistoryProbe(available=True)
    resources = NativeToolResources(visual_history_probe=probe)
    captured: dict[str, object] = {}
    model = MockAssistantChatModel()
    memory_backend = object()

    def fake_build_fast_agent(model, tools, **kwargs):
        captured.update(kwargs)
        return "fast-agent"

    async def fake_inventory(config, *, resources, mcp_server_configs):
        return []

    monkeypatch.setattr(
        services.ProviderConfig,
        "from_env",
        classmethod(lambda cls: ProviderConfig(provider_mode="mock")),
    )
    monkeypatch.setattr(
        services,
        "_compose_sync",
        lambda config, store: (model, resources, memory_backend),
    )
    monkeypatch.setattr(services, "create_native_tool_inventory", fake_inventory)
    monkeypatch.setattr(services, "load_mcp_server_configs_from_env", lambda: ())
    monkeypatch.setattr(services, "create_context_token_counter", lambda config: None)
    monkeypatch.setattr(services, "build_fast_agent", fake_build_fast_agent)
    monkeypatch.setattr(services, "build_planning_graph", lambda model, fast: "planning")
    monkeypatch.setattr(services, "build_assistant_root_graph", lambda **kwargs: "root")
    monkeypatch.setattr(services, "build_memory_extraction_graph", lambda **kwargs: "memory")

    asyncio.run(services.AgentServerExecutionOwner.compose(store=None))

    assert captured["visual_history_probe"] is probe


def test_visual_memory_tool_fails_closed_without_video_handshake(
    tmp_path,
) -> None:
    pool = SessionVisualSemanticStorePool(root=tmp_path / "semantic")
    visual_memory_tool = VisualMemorySearchTool(
        semantic_store_pool=pool,
        text_index=UnavailableVisualMemoryTextIndex(
            code="offline", message="offline sentinel"
        ),
    )
    runtime = native_tool_runtime(
        content=[{"type": "text", "text": "钥匙在哪里"}],
        context=AssistantRunContext(realtime_media_mode="none")
    )
    try:
        with pytest.raises(ToolException, match="video_handshake_required"):
            visual_memory_tool._run(query="钥匙", runtime=runtime)
    finally:
        pool.close()
```

测试不得调用真实 Provider 或外部 Qdrant；使用 in-memory fake probe 和 `UnavailableVisualMemoryTextIndex`。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py \
  -k 'fast_agent_receives or fails_closed'
```

Expected: FAIL，因为资源和执行保护尚未接通。

- [ ] **Step 3: 将 probe 穿透生产 composition**

Task 2 已为 `VisualPerceptionToolResources` 增加 probe。本 Task 为 `NativeToolResources` 增加同一字段：

```python
visual_history_probe: VisualObservationHistoryProbe | None = None
```

`ToolPluginContext` 不增加该字段，因为 probe 只服务 model exposure，不进入 Tool schema 或 Tool 执行。

`_compose_sync()` 从 `visual_perception.tool_resources()` 取得 probe；`AgentServerExecutionOwner.compose()` 调用：

```python
fast_agent = build_fast_agent(
    model,
    tools,
    model_call_limit=config.max_tool_iterations,
    tool_call_limit=config.max_tool_iterations,
    context_window_tokens=config.context_input_token_limit,
    compaction_trigger_ratio=config.context_compaction_trigger_ratio,
    compaction_target_ratio=config.context_compaction_target_ratio,
    token_counter=(
        context_token_counter.count_messages
        if context_token_counter is not None
        else None
    ),
    visual_history_probe=tool_resources.visual_history_probe,
)
```

- [ ] **Step 4: 声明 visual memory availability 并增加执行保护**

`VisualMemorySearchTool` 设置：

```python
availability = ToolAvailability.VISUAL_HISTORY_AVAILABLE
```

执行前检查 `ToolContext.metadata["realtime_media_mode"] == "video"`；不满足时返回失败 `ToolResult(error="video_handshake_required")`。查询发生时仍使用现有 authenticated `user_id`、runtime-owned `session_id` 和 as-of/time window，不能接受模型传入 owner。

`tools.base._tool_context()` 将 `runtime.context.realtime_media_mode` 写入 metadata，并从 `latest_runtime_media()` 写入可信 `visual_target_sequence`。

- [ ] **Step 5: 运行全部条件 TDD**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure
```

Expected: 全部 PASS，包括 VIDEO 握手后 live 可见、history 写入后 search 可见、断开/普通入口隐藏。

- [ ] **Step 6: 提交 Task 6**

```bash
git add \
  src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py \
  src/assistant_agent/native_agent/tools.py \
  src/assistant_agent/agent_server/services.py \
  src/assistant_agent/native_agent/fast_agent.py \
  src/assistant_agent/tools/base.py \
  tests/tdd/conditional-visual-tool-exposure/test_conditional_exposure.py
git diff --cached --check
git commit -m "feat: wire conditional visual tools into native agent"
```

### Task 7: 迁移文档、Skill 说明、smoke 并完成验证

**Files:**
- Create: `evals/system/tools/uploaded_media_inspect.py`
- Modify: `evals/system/tools/native_tool.py`
- Modify: `evals/system/tools/_smoke_runner.py`
- Modify: `evals/system/tools/live_view_inspect.py`
- Delete: `evals/system/tools/media_inspect.py`
- Modify: `skills/visual-context/SKILL.md`
- Modify: `skills/visual-context/skill.toml`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/multimodal-embedding-architecture.md`

**Interfaces:**
- Consumes: Tasks 1-6 的最终名称、握手语义、availability 和生命周期。
- Produces: 无旧生产名称的当前文档和离线 smoke 入口。

- [ ] **Step 1: 更新离线 Tool smoke**

`uploaded_media_inspect.py` 使用：

```python
from assistant_agent.media.vision.vision_client import MockVisionUnderstandingClient
from assistant_agent.tools.plugins.builtin.media_inspection.uploaded_tool import (
    create_uploaded_media_inspect_tool,
)

FIXED_INPUT = {"question": "图片里有什么？"}
FIXED_REQUEST = [
    {"type": "text", "text": "图片里有什么？"},
    {"type": "image", "id": "tool-smoke-image", "source": "uploaded"},
]

tool = create_uploaded_media_inspect_tool(MockVisionUnderstandingClient())
```

`live_view_inspect.py` 改为从 `live_view_tool` 导入，并给 video block 添加 `source="live_camera"`；smoke runtime context 使用 `realtime_media_mode="video"`。删除旧 `media_inspect.py`。

为 smoke harness 增加显式 context 透传：

```python
def invoke_native_tool(
    tool: BaseTool,
    arguments: dict[str, Any],
    *,
    user_identity: str,
    thread_id: str,
    tool_call_id: str,
    request_content: str | list[dict[str, Any]] | None = None,
    state: dict[str, Any] | None = None,
    context: AssistantRunContext | None = None,
) -> NativeToolInvocation:
    # graph.invoke 的 context 使用：
    resolved_context = context or AssistantRunContext(entry_profile="system_eval")
```

`run_tool_smoke()` 增加相同的可选 `context` 参数并原样传给 `invoke_native_tool()`。实时 smoke 调用：

```python
run_tool_smoke(
    tool,
    FIXED_INPUT,
    request_content=FIXED_REQUEST,
    context=AssistantRunContext(
        entry_profile="system_eval",
        realtime_media_mode="video",
    ),
)
```

- [ ] **Step 2: 更新 Skill 文案但不引入 Skill gate**

把 `skills/visual-context` 中的旧名称替换为 `uploaded_media_inspect`。保留 `activation="context"`、`discoverable=false`、`disable_model_invocation=true`；说明这三个 Tool 只在运行条件满足时出现，不要求 `load_skill`。

- [ ] **Step 3: 同步三个 authority**

- `tool-calling-architecture.md`：记录 Skill exposure 与 conditional exposure 是两个正交过滤层，三个视觉 Tool 只属于后者。
- `media-agent-service-websocket.md`：明确成功 VIDEO ACK 的完成语义、`realtime_media_mode` 投影和第一帧不是 live Tool 暴露前提。
- `multimodal-embedding-architecture.md`：更新新 Tool 名、持久 VLM client、视觉观察历史命名，以及 `visual_memory_search` 断线后不再暴露的新规则。

- [ ] **Step 4: 扫描旧名称和不一致描述**

Run:

```bash
if rg -n '\bmedia_inspect\b|断线后仍可查询|active_skill_ids.*visual' \
  src skills evals docs \
  --glob '!docs/superpowers/**'; then
  exit 1
fi
```

Expected: exit 0；不得残留旧 Tool 标识符、旧 import、旧 eval 文件、“断线后仍暴露 visual memory”的描述，或把视觉条件绑定到 Skill state 的实现。

- [ ] **Step 5: 运行离线 smoke、TDD 和邻接回归**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/tools/uploaded_media_inspect.py

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/tools/live_view_inspect.py

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/conditional-visual-tool-exposure \
  tests/tdd/progressive-native-tool-exposure \
  tests/tdd/realtime-visual-observation-service
```

Expected: 两个 smoke exit 0；全部 pytest PASS。

- [ ] **Step 6: 运行静态和文档权威检查**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/media/runtime_media.py \
  src/assistant_agent/media/visual_perception \
  src/assistant_agent/native_agent \
  src/assistant_agent/tools/plugins/builtin/media_inspection \
  tests/tdd/conditional-visual-tool-exposure

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .

git diff --check
```

Expected: 全部 exit 0；documentation authority 输出 valid。

- [ ] **Step 7: 核对 8089 单实例服务**

只连接现有 `8089`，不得启动第二套 Agent Server。等待 PyCharm 管理实例 reload 后检查其健康与进程来源；若当前实例是未挂载源码的旧容器镜像，只报告“未在 8089 验证新代码”，不要用并行服务伪造验证。至少执行：

```bash
curl --fail --silent http://127.0.0.1:8089/health/agent-server-adapter
```

Expected: 健康实例返回 `status=ok`；随后通过当前服务日志或 trace 确认其加载了 `uploaded_media_inspect`。无法确认镜像已更新时，把该项列为限制，不影响已完成的离线验证结论。

- [ ] **Step 8: 提交迁移与文档**

```bash
git add \
  evals/system/tools/uploaded_media_inspect.py \
  evals/system/tools/native_tool.py \
  evals/system/tools/_smoke_runner.py \
  evals/system/tools/live_view_inspect.py \
  evals/system/tools/media_inspect.py \
  skills/visual-context/SKILL.md \
  skills/visual-context/skill.toml \
  docs/tool-calling-architecture.md \
  docs/media-agent-service-websocket.md \
  docs/multimodal-embedding-architecture.md
git diff --cached --check
git commit -m "docs: document conditional visual tool exposure"
```

## Completion Report Contract

最终汇报必须包括：

```text
Core invariant: unchanged.
Tests: added tests/tdd/conditional-visual-tool-exposure for temporary RED/GREEN; user may delete the directory manually.
Provider: mock/local/offline only; no real Provider called.
Server: 8089 reload verification result, or explicit说明旧镜像限制。
```

同时列出实际执行的 pytest、smoke、ruff、compileall、documentation authority 和 `git diff --check` 命令，不得把未执行项写成通过。
