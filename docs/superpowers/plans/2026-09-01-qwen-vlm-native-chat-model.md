# Qwen VLM 原生 BaseChatModel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将后台关键帧窗口和用户上传图片的 Qwen 视觉理解统一迁移到 `BaseChatModel.invoke/ainvoke`，由 LangChain callback 自动生成唯一的原生 `vlm.infer` run。

**Architecture:** 扩展现有 `DashScopeNativeChatModel` 支持 `multimodal-generation` 与标准 LangChain 图片 blocks；Qwen Vision adapter 只构造消息并解析结构化结果。Qwen 调用使用预生成 `run_id` 的 `RunnableConfig` 直接产生原生 model run，非 Qwen 与 mock 保留现有手工 tracing fallback。

**Tech Stack:** Python 3.12、LangChain `BaseChatModel`/messages/callback、LangSmith native tracing、DashScope multimodal-generation HTTP API、Pydantic、pytest。

**Spec:** `docs/superpowers/specs/2026-09-01-qwen-vlm-native-chat-model-design.md`

## Global Constraints

- 后台视觉流水线仍独立于 LangGraph node，窗口继续并行，不复用 AssistantAgent state、Memory、Tool 或 checkpoint。
- Qwen Vision 只调用百炼原生 `multimodal-generation` endpoint；不得回退 Realtime、OpenAI-compatible 或 mock。
- 图片顺序固定为 `image[0..n-1] -> final prompt`，窗口最后一张始终是当前目标画面。
- prompt、Provider payload 与 LangSmith input 必须来自同一消息对象，不复制第二份 prompt 拼装逻辑。
- `AIMessage.content` 只展示 `summary`；完整结构化 JSON 保存在 `additional_kwargs["structured_output"]`。
- Provider 原始 response、API key、Authorization、真实路径和真实媒体不得写入 metadata、仓库或 artifact。
- 默认 pytest 与 TDD 使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；真实调用只能走 operator-gated system eval。
- 不新增第三方依赖，不引入 DashScope Python SDK，不修改 core invariant。
- 临时 RED/GREEN 测试只放在被 `.gitignore` 排除的 `tests/tdd/qwen-vlm-native-chat-model/`，不提交该目录。

---

### Task 1: 扩展 DashScope BaseChatModel 的原生多模态能力

**Files:**
- Modify: `src/assistant_agent/providers/dashscope_chat.py`
- Modify: `src/assistant_agent/providers/dashscope_langchain.py`
- Test: `tests/tdd/qwen-vlm-native-chat-model/test_dashscope_multimodal_chat_model.py`

**Interfaces:**
- Consumes: 现有 `UrllibDashScopeTransport`、`DashScopeNativeChatModel`、`_usage_metadata()` 和 DashScope message response。
- Produces: `DashScopeNativeChatModel(api_mode="multimodal", temperature=0.0)`；`dashscope_multimodal_generation_url(base_url: str) -> str`；multimodal `HumanMessage` 到 DashScope content 的有序转换。

- [ ] **Step 1: 写 endpoint、消息顺序、summary preview 和 usage 的失败测试**

```python
from langchain_core.messages import HumanMessage

from assistant_agent.providers.dashscope_langchain import DashScopeNativeChatModel


class RecordingTransport:
    def __init__(self) -> None:
        self.url = None
        self.payload = None

    def post_json(self, *, url, headers, payload, timeout_seconds):
        self.url = url
        self.payload = payload
        return {
            "request_id": "request-1",
            "output": {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": [{"text": (
                        '{"summary":"目标是水杯","objects":["水杯"],'
                        '"colors":["透明"]}'
                    )}]},
                }]
            },
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        }


def test_multimodal_chat_model_preserves_order_and_returns_summary_message():
    transport = RecordingTransport()
    model = DashScopeNativeChatModel(
        api_key="test-key",
        base_url="https://workspace.example/compatible-mode/v1",
        model_name="qwen3.6-flash",
        api_mode="multimodal",
        temperature=0.0,
        enable_thinking=False,
        http_transport=transport,
    )
    result = model.invoke([HumanMessage(content=[
        {"type": "image", "base64": "frame-1", "mime_type": "image/jpeg"},
        {"type": "image", "base64": "frame-2", "mime_type": "image/jpeg"},
        {"type": "text", "text": "最终完整 prompt"},
    ])])

    assert transport.url.endswith(
        "/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert transport.payload["input"]["messages"][0]["content"] == [
        {"image": "data:image/jpeg;base64,frame-1"},
        {"image": "data:image/jpeg;base64,frame-2"},
        {"text": "最终完整 prompt"},
    ]
    assert transport.payload["parameters"] == {
        "result_format": "message",
        "enable_thinking": False,
        "temperature": 0.0,
    }
    assert result.content == "目标是水杯"
    assert result.additional_kwargs["structured_output"]["objects"] == ["水杯"]
    assert result.usage_metadata == {
        "input_tokens": 10,
        "output_tokens": 4,
        "total_tokens": 14,
    }
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model/test_dashscope_multimodal_chat_model.py
```

Expected: FAIL，`DashScopeNativeChatModel` 尚不接受 `api_mode`/`temperature`，且只会调用 text-generation。

- [ ] **Step 3: 增加原生 multimodal endpoint helper**

在 `dashscope_chat.py` 中保留 text helper，并增加：

```python
def dashscope_multimodal_generation_url(base_url: str) -> str:
    return _dashscope_service_url(
        base_url,
        "/api/v1/services/aigc/multimodal-generation/generation",
    )
```

把当前 host 归一化提取为私有 `_dashscope_service_url()`，text 与 multimodal 共用；绝对 URL 校验和错误文案保持脱敏。

- [ ] **Step 4: 用最小字段扩展现有 BaseChatModel**

在 `DashScopeNativeChatModel` 增加：

```python
api_mode: Literal["text", "multimodal"] = "text"
temperature: float | None = None
```

`_generate()`/`_stream()` 通过一个 `_generation_url()` 选择 endpoint：

```python
def _generation_url(self) -> str:
    if self.api_mode == "multimodal":
        return dashscope_multimodal_generation_url(self.base_url)
    return dashscope_generation_url(self.base_url)
```

`_build_payload()` 仅在 `temperature is not None` 时写参数，确保主 Graph text 模式 payload 不漂移。

- [ ] **Step 5: 保留标准多模态 blocks 的顺序**

把 serializer 改成显式模式参数：

```python
def _message_to_dashscope(
    message: AnyMessage,
    *,
    multimodal: bool = False,
) -> dict[str, Any]:
    if isinstance(message, HumanMessage) and multimodal:
        return {
            "role": "user",
            "content": [_content_block_to_dashscope(block) for block in message.content],
        }
    # 原有 text/tool/assistant 分支保持不变
```

`_content_block_to_dashscope()` 只接受 `type=image|text`；image 必须同时包含非空 `base64` 和
`mime_type`，转换为 data URL；未知或不完整 block 直接 `ValueError`，不得静默丢帧。

- [ ] **Step 6: 在 multimodal 模式生成 summary preview**

在 `_parse_response()` 取得 Provider text 后：

```python
structured_output = _json_object_from_text(content)
summary = structured_output.get("summary")
if not isinstance(summary, str) or not summary.strip():
    raise DashScopeProviderError("DashScope multimodal response missing summary.")
return AIMessage(
    content=summary.strip(),
    additional_kwargs={"structured_output": structured_output},
    response_metadata=self._response_metadata(
        data,
        finish_reason=finish_reason,
        sources=sources,
    ),
    usage_metadata=_usage_metadata(data.get("usage")),
)
```

text 模式继续返回现有 AI/tool message，不解析视觉 JSON。

- [ ] **Step 7: 运行 Task 1 测试与现有 DashScope 定向测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model/test_dashscope_multimodal_chat_model.py \
  tests/tdd/dashscope-cache-usage \
  tests/tdd/dashscope-tool-call-streaming \
  tests/tdd/deep-agents-summarizer/test_main_model_output_limit.py
```

Expected: PASS；text 模式回归不变。

- [ ] **Step 8: 提交 Task 1**

```bash
git add \
  src/assistant_agent/providers/dashscope_chat.py \
  src/assistant_agent/providers/dashscope_langchain.py
git commit -m "feat: add native DashScope multimodal chat mode"
```

---

### Task 2: 将 Qwen Vision adapter 迁移到 BaseChatModel

**Files:**
- Modify: `src/assistant_agent/media/vision/vision_adapter.py`
- Modify: `src/assistant_agent/media/vision/vision_client.py`
- Modify: `src/assistant_agent/media/vision/real_vision_adapter.py`
- Modify: `src/assistant_agent/providers/provider_selection.py`
- Test: `tests/tdd/qwen-vlm-native-chat-model/test_qwen_vision_chat_adapter.py`

**Interfaces:**
- Consumes: Task 1 的 `DashScopeNativeChatModel(api_mode="multimodal")` 和标准 `HumanMessage` blocks。
- Produces: `VisionUnderstandingAdapter.understand(input, *, config=None)`；`VisionUnderstandingClient.understand(request, *, config=None)`；`DashScopeVisionProviderAdapter.traces_as_chat_model = True`。

- [ ] **Step 1: 写 adapter 只调用一次 model、保留图片顺序并共用最终 prompt 的失败测试**

```python
from langchain_core.messages import AIMessage

from assistant_agent.media.vision.real_vision_adapter import (
    DashScopeVisionProviderAdapter,
    RealVisionProviderConfig,
)
from assistant_agent.media.vision.vision_adapter import VisionUnderstandingInput


class RecordingModel:
    def __init__(self) -> None:
        self.messages = None
        self.config = None

    def invoke(self, messages, config=None):
        self.messages = messages
        self.config = config
        return AIMessage(
            content="目标是水杯",
            additional_kwargs={"structured_output": {
                "summary": "目标是水杯",
                "objects": ["水杯"],
                "colors": ["透明"],
            }},
        )


def test_qwen_vision_adapter_invokes_native_chat_model_once(tmp_path):
    first = tmp_path / "1.jpg"
    second = tmp_path / "2.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    model = RecordingModel()
    adapter = DashScopeVisionProviderAdapter(
        RealVisionProviderConfig(
            provider="qwen",
            api_key="test-key",
            base_url="https://workspace.example/compatible-mode/v1",
            model="qwen3.6-flash",
        ),
        chat_model=model,
    )

    result = adapter.understand(
        VisionUnderstandingInput(
            image_ids=[str(first), str(second)],
            question="最后一张是目标画面。",
        ),
        config={"run_name": "vlm.infer"},
    )

    blocks = model.messages[0].content
    assert [block["type"] for block in blocks] == ["image", "image", "text"]
    assert blocks[-1]["text"].startswith("最后一张是目标画面。")
    assert "objects: string[]" in blocks[-1]["text"]
    assert model.config["run_name"] == "vlm.infer"
    assert result.summary == "目标是水杯"
    assert result.objects == ["水杯"]
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model/test_qwen_vision_chat_adapter.py
```

Expected: FAIL，adapter 尚不接受 `chat_model`/`RunnableConfig`，仍直接调用 `urlopen`。

- [ ] **Step 3: 给 Vision adapter/client 增加可选 RunnableConfig 透传**

将 `VisionUnderstandingAdapter` protocol 与 image adapter 实现统一为以下方法签名：

```python
def understand(
    self,
    input: VisionUnderstandingInput,
    *,
    config: RunnableConfig | None = None,
) -> VisualUnderstandingResult:
    """Return one structured image-understanding result."""
    raise NotImplementedError
```

将 `VisionUnderstandingClient` protocol 与 `AdapterVisionUnderstandingClient` 统一为：

```python
def understand(
    self,
    request: VisionUnderstandingRequest,
    *,
    config: RunnableConfig | None = None,
) -> VisionUnderstandingResult:
    """Return one provider-neutral vision result."""
    raise NotImplementedError
```

`AdapterVisionUnderstandingClient` 只把 config 传给 image adapter；video adapter 路径不变。mock、HTTP 和 Ark
image adapters 接受该 keyword 后立即 `del config`，不改变其 Provider 行为。

- [ ] **Step 4: 把 Qwen Vision adapter 缩成消息构造与结果映射**

构造器接受可注入 model，默认创建：

```python
self.chat_model = chat_model or DashScopeNativeChatModel(
    api_key=config.api_key or "",
    base_url=config.base_url,
    model_name=config.model,
    api_mode="multimodal",
    temperature=0.0,
    enable_thinking=False,
    streaming=False,
    timeout_seconds=timeout_seconds,
)
```

`understand()` 使用现有图片 MIME/base64 helper 构造标准 blocks，末尾只追加一次公开
`vision_prompt(question)`，然后调用：

```python
message = self.chat_model.invoke([HumanMessage(content=content)], config=config)
structured = message.additional_kwargs.get("structured_output")
if not isinstance(structured, dict):
    raise ProviderAdapterError(
        "provider_bad_response",
        "DashScope multimodal response missing structured output",
    )
return map_vision_result(structured)
```

设置类事实 `traces_as_chat_model = True`，供调用边界选择原生 model tracing。

- [ ] **Step 5: 删除 Qwen adapter 重复 HTTP/response 代码**

从 `real_vision_adapter.py` 删除仅由旧 Qwen 路径使用的：

- `dashscope_multimodal_url()`；
- `build_dashscope_vision_payload()`；
- `parse_dashscope_vision_response()`；
- Qwen adapter 内的 `urllib` 请求和 response decode。

保留 OpenAI-compatible adapter、`image_to_data_url()`、`map_vision_result()` 和 JSON/schema helpers；公开
`vision_prompt()` 由 adapter 与测试共用。

- [ ] **Step 6: 运行 Task 2 与旧有序多图测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model \
  tests/tdd/dashscope-native-visual-window
```

Expected: PASS；旧测试中直接针对已删除 payload helper 的断言迁移到 Task 1 标准 messages/payload 测试，不保留重复断言。

- [ ] **Step 7: 提交 Task 2**

```bash
git add \
  src/assistant_agent/media/vision/vision_adapter.py \
  src/assistant_agent/media/vision/vision_client.py \
  src/assistant_agent/media/vision/real_vision_adapter.py \
  src/assistant_agent/providers/provider_selection.py
git commit -m "refactor: invoke Qwen vision through BaseChatModel"
```

---

### Task 3: 让 Qwen `vlm.infer` 由 LangChain callback 原生拥有

**Files:**
- Modify: `src/assistant_agent/media/vision/observability.py`
- Modify: `src/assistant_agent/media/visual_perception/observation_service.py`
- Modify: `src/assistant_agent/media/video/realtime_video_observer.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py`
- Test: `tests/tdd/qwen-vlm-native-chat-model/test_qwen_native_trace_routing.py`
- Test: `tests/tdd/native-visual-observability/test_native_visual_tracing.py`

**Interfaces:**
- Consumes: Task 2 的 `traces_as_chat_model` 和 client `config` 透传。
- Produces: Step 3 定义的 `invoke_native_vision_model()`；唯一原生 `vlm.infer` run config；
  `trace_visual_observation()` 的新 keyword `include_frame_attachments: bool = True`。

- [ ] **Step 1: 写原生 run config、精确 span ID 和无重复手工 span 的失败测试**

```python
from uuid import UUID

from assistant_agent.media.vision.observability import (
    VisionInferenceTraceContext,
    invoke_native_vision_model,
)


def test_native_model_config_owns_vlm_run_and_link():
    seen = {}
    links = []
    context = VisionInferenceTraceContext(
        trace_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        run_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )

    def call(config):
        seen["config"] = config
        return "ok"

    result = invoke_native_vision_model(
        call,
        context=context,
        capability="image_understanding",
        source="background_keyframe_observation",
        media_kind="live_view",
        media_count=3,
        trace_link_callback=links.append,
    )

    assert result == "ok"
    assert seen["config"]["run_name"] == "vlm.infer"
    assert isinstance(seen["config"]["run_id"], UUID)
    assert seen["config"]["tags"] == ["vlm"]
    assert links[0].span_id == str(seen["config"]["run_id"])
    assert context.last_link == links[0]
```

另写一个 fake native client 测试 `RealtimeVisualObservationService.observe()`：断言只收到一次
`client.understand(request, config=recorded_config)`，且 monkeypatched `trace()` 没有创建手工 `vlm.infer`。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model/test_qwen_native_trace_routing.py
```

Expected: FAIL，`invoke_native_vision_model` 与 native routing 尚不存在。

- [ ] **Step 3: 增加只生成 RunnableConfig/link 的窄 helper**

在 `observability.py` 增加：

```python
def invoke_native_vision_model(
    call: Callable[[RunnableConfig], _ResultT],
    *,
    context: VisionInferenceTraceContext | None,
    capability: str,
    source: str,
    media_kind: str,
    media_count: int,
    trace_link_callback: Callable[[VisionInferenceTraceLink], None] | None = None,
    **metadata: Any,
) -> _ResultT:
    run_id = uuid4()
    config: RunnableConfig = {
        "run_name": VISION_INFERENCE_OBSERVATION_NAME,
        "run_id": run_id,
        "tags": ["vlm"],
        "metadata": _vision_inference_metadata(
            capability=capability,
            source=source,
            media_kind=media_kind,
            media_count=media_count,
            extra=metadata,
        ),
    }
    if context is not None:
        link = VisionInferenceTraceLink(
            trace_id=context.trace_id,
            run_id=context.run_id,
            span_id=str(run_id),
        )
        context.last_link = link
        _notify_trace_link_fail_open(trace_link_callback, link)
    return call(config)
```

该 helper 不调用 `trace()`、RunTree 或 LangSmith client；真正 run 只能由 `BaseChatModel.invoke()` 的 callback 创建。
抽取 `_vision_inference_metadata()` 供它与非 Qwen 手工 fallback 共用现有安全 metadata。

- [ ] **Step 4: 在后台 observation service 选择原生或 fallback**

增加只读属性：

```python
@property
def traces_as_chat_model(self) -> bool:
    return bool(getattr(self._client, "traces_as_chat_model", False))
```

native 分支调用：

```python
raw = invoke_native_vision_model(
    lambda config: self._client.understand(provider_request, config=config),
    context=trace_context,
    capability="video_understanding",
    source="background_keyframe_observation",
    media_kind="live_view",
    media_count=len(request.frame_refs),
    trace_link_callback=trace_links.append,
    frame_sequence=request.frame_sequence,
    visual_window_id=request.visual_window_id,
    target_sequence=request.target_sequence,
    window_role=request.window_role,
)
```

非 native 分支继续调用现有 `observe_vision_inference()`。client 的 `traces_as_chat_model` 由 image adapter
事实透传，不根据 provider 名称字符串猜测。

- [ ] **Step 5: 在上传图片 Tool 使用相同 routing**

`UploadedMediaInspector._inspect_images()` 对 native client 使用 `invoke_native_vision_model()`；parent callback
由当前 Tool/Graph config 自动传播。显式视频仍走 `VideoUnderstandingBranch`，非 Qwen 图片仍走手工 fallback。

- [ ] **Step 6: 避免后台 JPEG 上传两遍**

给 `trace_visual_observation()` 增加：

```python
include_frame_attachments: bool = True
```

`_visual_attachments()` 在该值为 false 时只生成 `selected-keyframes-video`。`RealtimeVideoObserver` 已经先创建
isolated service，因此按 `not service.traces_as_chat_model` 传值：Qwen 图片由原生 `vlm.infer.messages`
展示，fallback 仍在 root 保留 JPEG。

- [ ] **Step 7: 更新观测测试并跑 GREEN**

更新 `test_native_visual_tracing.py`：

- fallback case 继续断言 root JPEG + MP4 和手工 child；
- native case 断言 root 只有 MP4、没有手工 child、link span ID 等于传给 model 的 run ID；
- 不断言完整自然语言 prompt，只断言标准 message block 类型、图片顺序和末尾 text block。

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model \
  tests/tdd/native-visual-observability
```

Expected: PASS。

- [ ] **Step 8: 提交 Task 3**

```bash
git add \
  src/assistant_agent/media/vision/observability.py \
  src/assistant_agent/media/visual_perception/observation_service.py \
  src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py
git commit -m "refactor: trace Qwen VLM as native model runs"
```

---

### Task 4: 同步 authority、system eval 与最终验证

**Files:**
- Modify: `docs/visual-perception-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `evals/system/realtime_visual_target_window/runner.py`（仅当 artifact 字段或断言仍假设手工 span/JPEG root attachments）
- Modify: `scripts/README.md`（仅当 system eval 输出契约变化）

**Interfaces:**
- Consumes: Tasks 1–3 完成的原生 Qwen `vlm.infer`。
- Produces: 当前 authority、离线验证证据、8089 hot reload 证据和一次 operator-gated 真实 trace。

- [ ] **Step 1: 更新视觉与可观测性 authority**

`docs/visual-perception-architecture.md` 明确：

- Qwen 图片理解通过 multimodal `BaseChatModel`；
- `vlm.infer.inputs.messages` 按序展示 JPEG 与最终 prompt；
- root 保留 MP4，Qwen JPEG 不重复作为 root attachment；
- summary 是默认 output content，完整结构在 details；
- 后台窗口仍是独立 root 和并行 task。

`docs/observability-harness.md` 明确：

- Qwen `vlm.infer` 由 LangChain callback 原生创建；
- 非 Qwen/manual fallback 不得造成重复 model run；
- run ID/link、Provider/model/usage 的诊断位置。

- [ ] **Step 2: 运行全部离线定向验证**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/qwen-vlm-native-chat-model \
  tests/tdd/dashscope-native-visual-window \
  tests/tdd/native-visual-observability \
  tests/tdd/dashscope-cache-usage \
  tests/tdd/dashscope-tool-call-streaming \
  tests/tdd/deep-agents-summarizer/test_main_model_output_limit.py
```

Expected: PASS。

- [ ] **Step 3: 运行共享边界回归与静态检查**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_observability_contract.py \
  tests/core/integration/test_runtime_lifecycle.py

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/providers/dashscope_chat.py \
  src/assistant_agent/providers/dashscope_langchain.py \
  src/assistant_agent/media/vision \
  src/assistant_agent/media/visual_perception/observation_service.py \
  src/assistant_agent/media/video/realtime_video_observer.py \
  src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py \
  evals/system/realtime_visual_target_window/runner.py

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .

git diff --check
```

Expected: pytest PASS、ruff PASS、authority `valid=true`、无 whitespace error。

- [ ] **Step 4: 运行 system eval dry-run**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py --dry-run
```

Expected: `network_called=false`，计划调用为一个关闭的 1～5 图窗口，检查项描述 native multimodal model run。

- [ ] **Step 5: 执行一次 operator-gated 真实 Qwen 验证**

仅在 `.env` 同时满足 real mode、Qwen chat/vision 完整配置并沿用用户已明确授权时执行。使用临时目录下载既有
LangSmith trace `01a05afc-41da-72c2-b8b9-ff8ff1fb1239` 的三张 keyframe，不保存到仓库；通过生产
`RealtimeVisualObservationService` 调用一次 `[键盘, 水杯, 水杯]`。

Expected:

- 只发生一次 DashScope multimodal Provider 请求；
- 最后一帧 summary 明确为水杯；
- 新 `vision.observation` 下只有一个 `vlm.infer` child；
- child inputs 顺序为三张图片后接最终完整 prompt；
- output content 是 summary，details 含完整结构化 JSON；
- provider/model/usage/run ID/parent 正确；
- root 只有 MP4 attachment，临时 JPEG 和 Provider response 不落盘到仓库。

- [ ] **Step 6: 验证唯一 8089 dev server hot reload**

等待现有 PyCharm 管理的 `8089` 实例 reload，禁止启动第二套 server。验证：

```bash
curl --silent --show-error --max-time 10 http://127.0.0.1:8089/ok
tail -n 120 /tmp/assistant_agent/logs/agent_server-8089.log
```

Expected: `/ok` 返回 `{"ok":true}`；日志出现本次 reload 后的 `Application started` 和
`native_graph_warmup_succeeded`，没有本次改造引入的 traceback。

- [ ] **Step 7: 提交 authority 与 eval 调整**

只 stage 本任务 hunk；若 authority 文件同时存在用户改动，使用交互式 hunk staging：

```bash
git add -p docs/visual-perception-architecture.md docs/observability-harness.md
git add evals/system/realtime_visual_target_window/runner.py scripts/README.md
git diff --cached --check
git commit -m "docs: document native Qwen VLM runs"
```

若 eval/script 文件没有实际变化，不 stage 空文件。最终报告必须包含：

```text
Core invariant: unchanged.
Tests: added/updated tests/tdd/qwen-vlm-native-chat-model for temporary RED/GREEN; user may delete the directory manually.
Real Provider: one operator-authorized Qwen multimodal window call; report model, latency, target-frame result, trace hierarchy, and attachment result without credentials or raw response.
```
