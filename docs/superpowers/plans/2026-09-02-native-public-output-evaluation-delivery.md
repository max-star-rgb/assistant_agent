# 原生公开输出、评测与 Tool 交付实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让生产 Graph 只公开原生对话结果，让 Experiment 保留完整公开结果，并由 ToolMessage artifact 统一声明必须确定性交付的文本和生成物。

**Architecture:** 使用 LangGraph `PrivateStateAttr` 隐藏项目内部 state，不增加 wrapper graph。内建 Tool 在已校验的领域 artifact 内写入统一 `assistant_agent_delivery_v1`，媒体边界只通用地消费当前轮每个 Tool 的最后一次成功结果。LangMem 直接使用官方 structured schema 阻止分类理由被写成记忆。

**Tech Stack:** Python 3.12、LangGraph/Deep Agents、LangChain `ToolMessage`、Pydantic v2、LangMem、pytest、Ruff。

**Spec:** `docs/superpowers/specs/2026-09-02-native-public-output-evaluation-delivery-design.md`

## Global Constraints

- 生产 Graph 仍由 `create_deep_agent` 直接编译，不增加 wrapper graph 或第二套 state channel。
- Experiment Outputs 保留完整公开 Graph result；single-step 继续来自 LangSmith 原生 child runs。
- Runtime 不识别 shopping、lodging、image generation 或 AMap Tool 名。
- 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`；不调用真实 Provider，不修改远程 evaluator，不删除历史 Memory。
- 不新增依赖；优先复用已有 Pydantic、ToolMessage 和生成物校验。

---

### Task 1: 私有 state 与完整 evaluation output

**Files:**
- Modify: `src/assistant_agent/native_agent/state.py`
- Modify: `src/assistant_agent/evaluation/native_graph_target.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`
- Create: `tests/tdd/native-public-output-delivery/test_evaluation_target.py`

**Interfaces:**
- Produces: `NativeGraphEvaluationResult.output: Mapping[str, Any]`
- Produces: `NativeGraphEvaluationResult.messages` 为从 `output` 派生的 property。

- [ ] **Step 1: 写失败测试**

```python
def test_result_keeps_complete_public_graph_output():
    output = {"messages": [AIMessage("answer")], "todos": []}
    result = NativeGraphEvaluationResult("thread", "run", output)
    assert result.output is output
    assert result.messages == tuple(output["messages"])

def test_compiled_output_schema_excludes_project_private_state(compiled_graph):
    fields = compiled_graph.get_output_jsonschema()["properties"]
    assert {"memory_context", "memory_status", "async_tasks"}.isdisjoint(fields)
    assert "messages" in fields
```

- [ ] **Step 2: 确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-public-output-delivery/test_evaluation_target.py tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_memory_lifecycle.py`

Expected: evaluation result 缺少 `output`，且公开 schema 仍含 Memory/异步任务字段。

- [ ] **Step 3: 最小实现**

```python
AsyncTasks = Annotated[dict[str, dict[str, JsonValue]], merge_async_tasks, PrivateStateAttr]
memory_context: NotRequired[Annotated[tuple[str, ...], PrivateStateAttr]]
memory_status: NotRequired[Annotated[MemoryStatus, PrivateStateAttr]]

@dataclass(frozen=True)
class NativeGraphEvaluationResult:
    thread_id: str
    run_id: str
    output: Mapping[str, Any]

    @property
    def messages(self) -> tuple[AnyMessage, ...]:
        return tuple(self.output.get("messages", ()))
```

调整已有 Memory core 断言：终态返回值不再含私有字段，改从 checkpoint/state snapshot 验证它们仍可用。

- [ ] **Step 4: 确认 GREEN**

Run 与 Step 2 相同，Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/native_agent/state.py src/assistant_agent/evaluation/native_graph_target.py tests/core/integration/test_runtime_lifecycle.py tests/core/integration/test_memory_lifecycle.py tests/tdd/native-public-output-delivery/test_evaluation_target.py
git commit -m "fix: expose only native public graph output"
```

### Task 2: 统一 Tool delivery artifact 与通用 Runtime

**Files:**
- Create: `src/assistant_agent/tools/delivery.py`
- Modify: `src/assistant_agent/agent_server/turn_delivery.py`
- Delete: `src/assistant_agent/agent_server/shopping_detail.py`
- Modify: `tests/tdd/deterministic-tool-delivery/test_turn_delivery.py`

**Interfaces:**
- Produces: `DELIVERY_ARTIFACT_KEY = "assistant_agent_delivery_v1"`
- Produces: `ToolDeliveryArtifact(text: str = "", output_refs: tuple[str, ...] = ())`
- Produces: `with_tool_delivery(artifact, *, text="", output_refs=()) -> dict[str, Any]`
- Produces: `read_tool_delivery(artifact) -> ToolDeliveryArtifact | None`

- [ ] **Step 1: 把现有业务 fixture 改为统一 artifact 契约**

```python
artifact = {
    DELIVERY_ARTIFACT_KEY: {
        "text": "<detail>\n1. sentinel <link>https://example.test/p</link>\n</detail>",
        "output_refs": [],
    }
}
```

断言当前轮、同名最后一次、最后失败不回退、MCP `structured_content`、标准 file block、畸形输入和去重上限。

- [ ] **Step 2: 确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/deterministic-tool-delivery/test_turn_delivery.py`

Expected: 缺少统一 delivery model/helper，并且旧 Runtime 仍按业务 Tool 名解析。

- [ ] **Step 3: 实现最小统一契约**

```python
class ToolDeliveryArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(default="", max_length=16_000)
    output_refs: tuple[str, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def non_empty(self):
        if not self.text and not self.output_refs:
            raise ValueError("tool delivery must not be empty")
        return self
```

`turn_delivery()` 只做：截取当前轮、逆序选每个非空 `ToolMessage.name` 的最后一条、过滤失败、解析统一 artifact/file block、按原顺序去重合并。

- [ ] **Step 4: 删除领域感知实现并确认 GREEN**

Run: `rg -n "shopping|lodging|image_generation|AMAP|maps_direction" src/assistant_agent/agent_server/turn_delivery.py`

Expected: 无命中。

Run: Step 2 命令，Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/tools/delivery.py src/assistant_agent/agent_server/turn_delivery.py src/assistant_agent/agent_server/shopping_detail.py tests/tdd/deterministic-tool-delivery/test_turn_delivery.py
git commit -m "refactor: consume tool delivery artifacts generically"
```

### Task 3: 由各 Tool 写入确定性 delivery

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`
- Modify: `src/assistant_agent/mcp/amap_route_links.py`
- Modify: `tests/tdd/langgraph-native-tools/test_domain_query_tools.py`
- Modify: `tests/tdd/image-generation-studio-link/test_image_generation_output.py`
- Modify: `tests/tdd/deterministic-tool-delivery/test_turn_delivery.py`

**Interfaces:**
- Consumes: `with_tool_delivery(...)` 和 `DELIVERY_ARTIFACT_KEY`。
- Produces: 已校验 Tool artifact 中的统一 delivery envelope。

- [ ] **Step 1: 为四个 writer 增加 RED 断言**

```python
assert message.artifact[DELIVERY_ARTIFACT_KEY]["text"].startswith("<detail>")
assert message.artifact[DELIVERY_ARTIFACT_KEY]["output_refs"] == expected_refs
assert result.structuredContent[DELIVERY_ARTIFACT_KEY]["text"].startswith("[打开高德地图导航]")
```

- [ ] **Step 2: 确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-tools/test_domain_query_tools.py tests/tdd/image-generation-studio-link/test_image_generation_output.py tests/tdd/deterministic-tool-delivery/test_turn_delivery.py`

Expected: Tool artifact 仍没有统一 key，AMap 仍写旧 key。

- [ ] **Step 3: 在 Tool 边界构造 delivery**

```python
data = with_tool_delivery(data, text=_shopping_detail(result))
data = with_tool_delivery(data, text=_lodging_detail(result))
artifact = with_tool_delivery(artifact, output_refs=generated_refs)
structured_content[DELIVERY_ARTIFACT_KEY] = ToolDeliveryArtifact(text=link).model_dump(mode="json")
```

购物/酒店渲染函数与已校验领域 model 放在各自 `tool.py`；删除购物 Tool description 中要求 LLM 复述 `product_url` 的补丁。

- [ ] **Step 4: 确认 GREEN**

Run 与 Step 2 相同，Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/tools/plugins/builtin/shopping/tool.py src/assistant_agent/tools/plugins/builtin/lodging/tool.py src/assistant_agent/tools/plugins/builtin/image_generation/tool.py src/assistant_agent/mcp/amap_route_links.py tests/tdd
git commit -m "feat: declare deterministic delivery in tool artifacts"
```

### Task 4: 用 LangMem 原生 structured schema 收紧写入

**Files:**
- Modify: `src/assistant_agent/native_agent/memory.py`
- Modify: `tests/core/integration/test_memory_lifecycle.py`

**Interfaces:**
- Produces: `DurableMemory(content: str, kind: Literal["stable_fact", "preference", "long_term_goal", "reusable_procedure"])`
- Consumes: LangMem `create_memory_store_manager(..., schemas=[DurableMemory])`。

- [ ] **Step 1: 为 MEMORY-001 增加结构化装配断言**

```python
assert manager_kwargs["schemas"] == [DurableMemory]
assert DurableMemory.model_validate({"content": "sentinel", "kind": "stable_fact"})
```

- [ ] **Step 2: 确认 RED**

Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_memory_lifecycle.py`

Expected: manager 未传入 `schemas`。

- [ ] **Step 3: 最小实现**

```python
class DurableMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str = Field(min_length=1, max_length=4_000)
    kind: Literal["stable_fact", "preference", "long_term_goal", "reusable_procedure"]

return create_manager(..., schemas=[DurableMemory])
```

同时精简 instruction：无长期价值时不 insert，不把“不构成记忆”之类判定理由写入 `content`。

- [ ] **Step 4: 确认 GREEN**

Run 与 Step 2 相同，Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/assistant_agent/native_agent/memory.py tests/core/integration/test_memory_lifecycle.py
git commit -m "fix: constrain langmem to durable structured memories"
```

### Task 5: authority、实验矩阵与整体验证

**Files:**
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`

**Interfaces:**
- Documents: production output、checkpoint、Experiment Outputs、Trace、delivery 和 Memory 的唯一权威边界。

- [ ] **Step 1: 同步 authority**

明确 `messages/todos/structured_response` 是公开 result，项目 state 是私有 checkpoint；evaluation result 保留完整公开 output；Tool-owned namespaced delivery 由 transport 通用投影；LangMem 用 structured schema。

- [ ] **Step 2: 执行离线实验矩阵**

| 实验 | 输入 | 成功条件 |
| --- | --- | --- |
| 公开 schema | mock 编译 graph | 仅有原生公开字段 |
| checkpoint | memory-enabled mock run | 私有 Memory 可从 snapshot 读取 |
| evaluation target | scripted graph 返回 messages+todos | `output` 无损保留 |
| delivery | shopping/lodging/AMap/file/image fixture | LLM 不含链接仍得到确定输出 |
| failure | 同名 Tool 成功后失败 | 不回退旧 artifact |
| Memory | fake LangMem factory | 收到 `schemas=[DurableMemory]` |

- [ ] **Step 3: 运行完整离线验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/native-public-output-delivery tests/tdd/deterministic-tool-delivery tests/tdd/langgraph-native-tools/test_domain_query_tools.py tests/tdd/image-generation-studio-link/test_image_generation_output.py
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check src tests evals
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/evaluation
```

- [ ] **Step 4: 验证 8089 hot reload**

检查唯一 `langgraph dev` 8089 进程和对应日志；使用 mock 读取公开 schema/健康状态，不启动第二个 Server。

- [ ] **Step 5: 代码审查、最终提交与完成审计**

```bash
git diff --check
git status --short
git log -5 --oneline
```

只提交本任务文件，排除用户的 `.run/Agent Server (Real).run.xml`。真实 LangSmith shadow Experiment 保留为后续 operator 明确授权的 real-mode 验证，不在本次离线实施中执行。
