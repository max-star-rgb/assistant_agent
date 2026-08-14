# 视觉感知模块重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将实时 Observer、VLM client、视觉语义 Store 和并发治理收敛到 Agent Server 内部统一的 Visual Perception Module，并让实时 Tool 通过模块结果支持 latest 与冻结最后一帧的 strict 语义。

**Architecture:** `VisualPerceptionModule` 是进程级视觉资源 owner，`VisualPerceptionSession` 是连接级观察句柄；Media route 解码后异步提交帧，chat 到达时冻结并 promote 目标帧。生产 Tool 仍是标准 `BaseTool -> ToolNode`，只通过模块共享的 Store 读取结果，不建立第二套 Agent loop。

**Tech Stack:** Python 3.11、asyncio、LangGraph Agent Server、LangChain BaseTool、Pydantic、pytest。

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- thread/run/checkpoint/cancel/stream 仍由 Agent Server 原生拥有。
- 原始 H.264、JPEG 正文、Provider 原始响应不得进入 Graph State。
- `user_id + native thread_id` 是视觉会话隔离键；目标帧序号只能由媒体入口冻结并通过可信 context 注入。
- 临时 RED/GREEN 测试只放在 `tests/tdd/visual-perception-module/`。

---

### Task 1: 建立 Visual Perception Module 生命周期边界

**Files:**
- Create: `src/assistant_agent/media/visual_perception/__init__.py`
- Create: `src/assistant_agent/media/visual_perception/module.py`
- Test: `tests/tdd/visual-perception-module/test_visual_perception_module.py`

**Interfaces:**
- Consumes: `RealtimeVideoObserver`、`SessionEmbeddingCoordinatorStore`、`SessionVisualSemanticStorePool`、`RealtimeVideoMemoryStore`。
- Produces: `VisualPerceptionModule.open_session(user_id, session_id)`、`VisualPerceptionSession.submit(frame)`、`prepare_strict_target(video_ids)`、`aclose()` 以及模块拥有的 Tool resources。

- [ ] **Step 1: 写模块归属和会话生命周期 RED 测试**

```python
session = module.open_session(user_id="user-1", session_id="thread-1")
await session.submit(frame)
target = await session.prepare_strict_target(["video-1"])
assert target.sequence == frame.sequence
await session.aclose()
assert observer.closed is True
```

- [ ] **Step 2: 显式运行测试并确认因模块不存在而失败**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module/test_visual_perception_module.py`

Expected: FAIL，缺少 `assistant_agent.media.visual_perception`。

- [ ] **Step 3: 实现最小模块与连接级 session handle**

```python
class VisualPerceptionSession:
    async def submit(self, frame: VideoFrame) -> FrameProcessingResult: ...
    async def prepare_strict_target(self, video_ids: Sequence[str]) -> VisualTarget | None: ...
    async def aclose(self) -> None: ...

class VisualPerceptionModule:
    def open_session(self, *, user_id: str, session_id: str) -> VisualPerceptionSession: ...
    async def aclose(self) -> None: ...
```

- [ ] **Step 4: 运行模块测试并确认通过**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module/test_visual_perception_module.py`

Expected: PASS。

### Task 2: 将 Media route 接入模块并冻结最后一帧

**Files:**
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Modify: `src/assistant_agent/agent_server/media_session.py`
- Modify: `src/assistant_agent/native_agent/context.py`
- Test: `tests/tdd/visual-perception-module/test_media_visual_perception_wiring.py`

**Interfaces:**
- Consumes: `VisualPerceptionModule` 和 `VisualPerceptionSession`。
- Produces: 视频帧后台提交、chat 时的 `visual_target_sequence` 可信 context、连接关闭时 session cleanup。

- [ ] **Step 1: 写媒体接入 RED 测试**

```python
await visual_session.submit(decoded_frame)
await _run_chat(..., visual_target_sequence=7)
assert client.stream_kwargs["context"]["visual_target_sequence"] == 7
```

- [ ] **Step 2: 运行测试并确认目标序号尚未进入 native context**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module/test_media_visual_perception_wiring.py`

Expected: FAIL，缺少视觉 session 提交或 `visual_target_sequence`。

- [ ] **Step 3: 接入视频提交、strict target promotion 与结构化 context**

```python
target = await visual_session.prepare_strict_target(session.video_ids)
context = {
    "user_id": session.user_id,
    "tenant_id": "media-service",
    "entry_profile": "agent_service",
    "media_capabilities": list(session.media_capabilities),
    "visual_target_sequence": target.sequence if target else None,
}
```

- [ ] **Step 4: 运行媒体接入测试并确认通过**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module/test_media_visual_perception_wiring.py`

Expected: PASS。

### Task 3: 让生产 Tool 只消费模块语义结果

**Files:**
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Test: `tests/tdd/visual-perception-module/test_live_view_module_boundary.py`

**Interfaces:**
- Consumes: 模块的 `video_context_store`、`realtime_video_memory_store`、`visual_semantic_store_pool` 与可信 `visual_target_sequence`。
- Produces: `latest` 立即读取；存在目标序号时 `strict` 有界等待；Agent-Service live Tool 不直接调用 VLM client。

- [ ] **Step 1: 写 Tool 边界 RED 测试**

```python
context = ToolContext(metadata={"entry_profile": "agent_service", "visual_target_sequence": 4})
result = live_view_tool.run_legacy({"video_ids": ["video-1"], "query": "现在是什么？"}, context)
assert result.data["target_sequence"] == 4
assert raising_vlm_client.called is False
```

- [ ] **Step 2: 运行测试并确认当前 Tool 仍走直接 VLM 路径**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module/test_live_view_module_boundary.py`

Expected: FAIL，当前 native ToolContext 未被识别为 Agent-Service strict 查询。

- [ ] **Step 3: 注入模块共享资源并修正可信上下文读取**

```python
resources = visual_perception.tool_resources()
NativeToolResources(
    video_context_store=resources.video_context_store,
    realtime_video_memory_store=resources.realtime_video_memory_store,
    visual_semantic_store_pool=resources.visual_semantic_store_pool,
)
```

- [ ] **Step 4: 运行 Tool 边界测试并确认通过**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module/test_live_view_module_boundary.py`

Expected: PASS。

### Task 4: 同步权威文档并完成验证

**Files:**
- Modify: `docs/multimodal-embedding-architecture.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/authority.toml`

**Interfaces:**
- Consumes: Tasks 1–3 的最终生产行为。
- Produces: Visual Perception Module owner、Media/Tool 调用关系、strict/latest 阻塞语义的唯一当前描述。

- [ ] **Step 1: 更新 authority contract 和数据流**

明确记录：Observer 属于 Visual Perception Module；Tool 是薄消费入口；strict 模式冻结目标帧并有界等待，latest 模式不等待新推理。

- [ ] **Step 2: 运行 feature TDD 集合**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/visual-perception-module`

Expected: PASS。

- [ ] **Step 3: 运行相邻 Agent Server 与 Tool 契约验证**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_gateway_contract.py tests/core/contract/test_tool_contract.py`

Expected: PASS。

- [ ] **Step 4: 运行文档 authority 校验与静态编译**

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`

Run: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/media/visual_perception src/assistant_agent/agent_server`

Expected: 两条命令均退出码 0。
