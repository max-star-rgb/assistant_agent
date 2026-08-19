# 生图 Tool 原生 Artifact 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除图片专用父图投影节点，让 `image_generation` 通过原生 `ToolMessage.content + artifact` 双通道分别向模型和程序交付文本观察与结构化本地图片 URL。

**Architecture:** 保持现有 `BaseTool(response_format="content_and_artifact")`，不修改 ToolBase 通用契约。`ImageGenerationTool` 在成功物化 Provider URL 后向 artifact 增加一一对应的 `images[]` 描述；父图不再改写 AIMessage，普通客户端和 Media WS 从当前轮次 ToolMessage artifact 消费图片。

**Tech Stack:** Python 3.12、LangChain `BaseTool`/`ToolMessage`、LangGraph `create_agent`/`ToolNode`、Pydantic v2、FastAPI、pytest。

**Spec:** `docs/superpowers/specs/2026-08-18-native-tool-image-artifact-design.md`

## Global Constraints

- 默认使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实图片 Provider、主模型或外部服务。
- `ToolMessage.content` 不包含 URL、Base64、文件路径或 image block；图片只存在于 artifact。
- artifact 使用单一 `images[]` 对象列表，不新增 `output_refs` 平行数组。
- `images[].url` 只由可信 `artifact_base_url` 与受管 `output_ref` 构造。
- Media WS wire、ACK、delivery ID、Base64 IMAGE detail 和 3D callback 不变。
- 当前工作区存在用户未提交且重叠的改动；执行期间不创建 worktree、不提交、不回滚任何既有改动。

---

### Task 1: Image Tool 结构化 artifact

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/models.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py`
- Create: `tests/tdd/native-tool-image-artifact/test_image_tool_artifact.py`

**Interfaces:**
- Consumes: `ProviderConfig.artifact_base_url: str | None`、物化后的 `ImageGenerationResult.download_urls`。
- Produces: `GeneratedImageArtifact` 与 `ToolResult.data["images"]: list[dict[str, str]]`。

- [ ] **Step 1: 写 artifact 契约 RED 测试**

```python
def test_success_keeps_urls_out_of_model_observation() -> None:
    result = ImageGenerationResult(
        task_id="task-1",
        status="succeeded",
        image_id=["image-a"],
        image_url="/artifacts/generated/image-a.png",
        image_urls=["/artifacts/generated/image-a.png"],
        download_url="/artifacts/generated/image-a.png",
        download_urls=["/artifacts/generated/image-a.png"],
        output_ref="/artifacts/generated/image-a.png",
        prompt="画一只猫",
    )
    tool_result = ImageGenerationTool(
        adapter=StaticImageAdapter(result),
        artifact_base_url="http://127.0.0.1:8089",
    )._execute(ImageGenerationRequest(prompt="画一只猫"), ToolContext())

    assert tool_result.model_observation == {"image_id": ["image-a"]}
    assert tool_result.data["images"] == [{
        "image_id": "image-a",
        "output_ref": "/artifacts/generated/image-a.png",
        "url": "http://127.0.0.1:8089/artifacts/generated/image-a.png",
        "mime_type": "image/png",
    }]
    assert "url" not in json.dumps(tool_result.model_observation)
```

同文件增加：无 base URL 时 `url` 字段省略；非法/非受管引用不进入 `images[]`；重复引用去重且最多四项。

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-tool-image-artifact/test_image_tool_artifact.py
```

Expected: FAIL，因为 `GeneratedImageArtifact`、`artifact_base_url` 构造参数和 `data.images` 尚不存在。

- [ ] **Step 3: 实现最小 artifact builder**

在 `models.py` 增加：

```python
class GeneratedImageArtifact(BaseModel):
    image_id: str = Field(min_length=1)
    output_ref: str = Field(min_length=1)
    url: str | None = None
    mime_type: str = Field(min_length=1)
```

在 `ImageGenerationTool` 保存规范化后的 `artifact_base_url`，并让 `_image_generation_output_contract()` 接收它。
新增 `_generated_image_artifacts(result, artifact_base_url)`：只接受受管单层引用，按顺序去重、截断四项，用后缀映射 MIME，
并以 `model_dump(exclude_none=True)` 写入 `data["images"]`。Plugin 构造 Tool 时传入
`context.config.artifact_base_url`。

- [ ] **Step 4: 运行 GREEN 测试与 Ruff**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-tool-image-artifact/test_image_tool_artifact.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/plugins/builtin/image_generation/models.py \
  src/assistant_agent/tools/plugins/builtin/image_generation/tool.py \
  src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py \
  tests/tdd/native-tool-image-artifact/test_image_tool_artifact.py
```

Expected: 全部通过。

### Task 2: 删除父图 AIMessage 图片投影

**Files:**
- Modify: `src/assistant_agent/native_agent/root_graph.py`
- Modify: `src/assistant_agent/agent_server/services.py`
- Modify: `tests/tdd/studio-generated-image/test_studio_generated_image.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`

**Interfaces:**
- Consumes: Task 1 的 Tool artifact；父图不再消费图片。
- Produces: fast/planning 直接汇合到 `refresh_memory_extraction` 的父图拓扑。

- [ ] **Step 1: 把现有 Studio 临时测试改成新行为并运行 RED**

将原 `test_parent_graph_projects_generated_image_into_standard_ai_message` 改为：

```python
def test_parent_graph_does_not_rewrite_final_ai_message() -> None:
    graph = build_assistant_root_graph(
        memory_backend=DisabledMemoryBackend(),
        fast_agent=_image_branch(FastAgentState, "AssistantFastAgent"),
        planning_graph=_image_branch(PlanningState, "AssistantPlanningGraph"),
    )
    result = asyncio.run(graph.ainvoke(...))
    assert result["messages"][-1].content == "图片已生成。"
    assert "project_generated_images" not in graph.get_graph().nodes
```

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/studio-generated-image/test_studio_generated_image.py::test_parent_graph_does_not_rewrite_final_ai_message
```

Expected: FAIL，因为父图仍包含投影节点并改写 AIMessage。

- [ ] **Step 2: 删除父图投影节点**

从 `root_graph.py` 删除 `project_generated_images` import、node、两条入边、出边和导出；fast/planning 直接连接
`refresh_memory_extraction`。删除 `build_assistant_root_graph(..., artifact_base_url=...)` 参数，并从 `services.py`
移除对应传参。

- [ ] **Step 3: 更新已登记 LOOP-001 拓扑断言**

从 `tests/core/integration/test_runtime_lifecycle.py` 的节点集合中删除 `project_generated_images`，保留同一个
`@pytest.mark.core_invariant("LOOP-001")`，不新增 core 测试文件。

- [ ] **Step 4: 运行定向 GREEN 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/studio-generated-image \
  tests/core/integration/test_runtime_lifecycle.py::test_parent_graph_has_fast_and_planning_native_branches
```

Expected: 全部通过，最终 AIMessage 保持纯文本。

### Task 3: Media WS 读取 `artifact.images[]`

**Files:**
- Modify: `src/assistant_agent/runtime/generated_artifacts.py`
- Delete: `src/assistant_agent/native_agent/generated_images.py`
- Modify: `src/assistant_agent/agent_server/media_app.py`
- Create: `tests/tdd/native-tool-image-artifact/test_media_artifact_projection.py`
- Modify: `tests/tdd/studio-generated-image/test_studio_generated_image.py`

**Interfaces:**
- Consumes: `ToolMessage.artifact.images[].output_ref`，以及旧 checkpoint 的 `download_urls/output_ref`。
- Produces: `generated_image_output_refs(messages: Sequence[Any]) -> list[str]`。

- [ ] **Step 1: 写新旧 artifact 读取 RED 测试**

```python
def test_reads_current_turn_structured_images_before_legacy_fields() -> None:
    messages = [
        HumanMessage(content="生成图片"),
        ToolMessage(
            content='{"image_id":["image-a"]}',
            name="image_generation",
            tool_call_id="call-a",
            status="success",
            artifact={
                "status": "succeeded",
                "images": [{
                    "image_id": "image-a",
                    "output_ref": "/artifacts/generated/image-a.png",
                    "url": "http://127.0.0.1:8089/artifacts/generated/image-a.png",
                    "mime_type": "image/png",
                }],
                "download_urls": ["/artifacts/generated/legacy.png"],
            },
        ),
        AIMessage(content="图片已生成。"),
    ]
    assert generated_image_output_refs(messages) == [
        "/artifacts/generated/image-a.png"
    ]
```

同文件验证：没有 `images[]` 时读取旧 `download_urls/output_ref`；跨 HumanMessage 不聚合；失败、非法引用、重复和
超过四张均被过滤。

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-tool-image-artifact/test_media_artifact_projection.py
```

Expected: FAIL，因为共享 helper 尚未迁移且不会优先读取 `images[]`。

- [ ] **Step 3: 迁移 helper 并切换 Media adapter**

把 `generated_image_output_refs()` 移入 `runtime/generated_artifacts.py`，优先解析有效 `artifact.images[]`；仅当该字段
缺失时回退旧 `download_urls/output_ref`。`media_app.py` 改为从 runtime 模块导入。确认无其他引用后删除
`native_agent/generated_images.py`。

- [ ] **Step 4: 验证 WS 响应与图片 route**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-tool-image-artifact/test_media_artifact_projection.py \
  tests/tdd/studio-generated-image/test_studio_generated_image.py::test_media_projection_keeps_generated_image_refs_for_ws_delivery \
  tests/tdd/studio-generated-image/test_studio_generated_image.py::test_generated_artifact_route_serves_only_managed_images
```

Expected: 全部通过，wire 仍得到相同受管 refs 和文件内容。

### Task 4: Authority 同步与整体验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/agent-server-architecture.md`（仅当其当前内容仍声明父图图片投影）

**Interfaces:**
- Consumes: Tasks 1–3 已通过的最终行为。
- Produces: 与源码一致的当前 authority 和验证记录。

- [ ] **Step 1: 同步事实权威**

文档明确：图片 URL 位于 `ToolMessage.artifact.images[]`；content 只给 LLM；父图无图片节点；普通客户端与 WS
分别读取 `url` 与 `output_ref`；Studio 不显示是已知 UI 限制。

- [ ] **Step 2: 运行完整定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-tool-image-artifact \
  tests/tdd/studio-generated-image \
  tests/core/integration/test_runtime_lifecycle.py::test_parent_graph_has_fast_and_planning_native_branches
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/plugins/builtin/image_generation \
  src/assistant_agent/runtime/generated_artifacts.py \
  src/assistant_agent/native_agent/root_graph.py \
  src/assistant_agent/agent_server/media_app.py \
  tests/tdd/native-tool-image-artifact \
  tests/tdd/studio-generated-image
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
```

Expected: pytest 全部通过、Ruff 无错误、authority validator 返回 `errors=[]`。

- [ ] **Step 3: 验证 8089 热重载与现有 artifact route**

确认 `/tmp/assistant_agent/logs/agent_server-8089.log` 出现本次源码 reload 和成功 startup，然后请求一个现有受管图片：

```bash
curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' \
  -H 'x-auth-scheme: langsmith' \
  http://127.0.0.1:8089/artifacts/generated/349cc6c272f4ec7a88800f0f.png
```

Expected: `200 image/png`。不创建新 dev server，不调用真实 Provider。

- [ ] **Step 4: 复核 diff 与交付说明**

运行 `git diff --check`，确认没有回滚工作区中与本任务无关的修改。报告：Core invariant 为 LOOP-001 拓扑断言同步，
临时 TDD 目录可由用户手动删除；本轮未调用真实 Provider，且因重叠 dirty worktree 不提交。
