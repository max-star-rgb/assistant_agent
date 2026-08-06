# 工具重复执行策略统一分类实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有内置工具和 MCP 工具使用统一的 `ToolSpec.repeat_policy` 分类，并保证 `distinct_inputs` 只允许不同规范化参数重复执行。

**Architecture:** 工具实现通过公共声明契约把策略投影到 `ToolSpec`；内置工具逐类显式声明，MCP 根据可信 `is_read_only` 配置映射。assistant loop 移除图片生成名称特判，并在决策与执行两个边界使用通用成功记录阻止相同输入重复执行。

**Tech Stack:** Python 3.11、Pydantic v2、pytest、现有 `AgentGraphRuntime` assistant loop 与 Tool Registry。

## Global Constraints

- 只使用 `once_per_run` 与 `distinct_inputs`，不新增第三种策略。
- 所有 pytest 使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不调用真实 Provider 或网络。
- RED/GREEN 只修改 `tests/tdd/tool-repeat-policy/`；不修改 `tests/core`，Core invariant 保持不变。
- 所有显式工具调用继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- 不根据用户话术、工具名称或正则推断策略；MCP 只读取结构化 `is_read_only` 配置。
- 保留用户工作区中的既有改动；对已修改文件只应用本任务的窄补丁。
- 新增 spec 与 plan 默认不提交；实现完成后再根据 dirty worktree 是否能安全隔离本任务改动决定是否提交。

---

### Task 1: 公共重复策略声明与 MCP 映射

**Files:**
- Modify: `tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`
- Modify: `src/assistant_agent/tools/base.py`
- Modify: `src/assistant_agent/tools/decorators.py`
- Modify: `src/assistant_agent/mcp/adapter.py`

**Interfaces:**
- Consumes: `ToolRepeatPolicy = Literal["once_per_run", "distinct_inputs"]` from `assistant_agent.tools.models`。
- Produces: `Tool.repeat_policy: ToolRepeatPolicy`、`ToolBase.repeat_policy = "once_per_run"`、decorator 参数 `repeat_policy: ToolRepeatPolicy = "once_per_run"`，以及 MCP definition/proxy 的一致策略映射。

- [ ] **Step 1: 写 MCP 与公共声明的失败测试**

在现有 TDD 文件中增加真实 Registry/adapter 行为测试：

```python
def test_mcp_read_and_write_definitions_project_matching_repeat_policy() -> None:
    config = MCPToolAdapterConfig(
        server_name="server-sentinel",
        allowed_tools=["lookup", "mutate"],
        read_only_tools=["lookup"],
    )
    adapter = MCPToolAdapter(config)

    read_spec = adapter.tool_spec_for_definition(MCPToolDefinition(name="lookup"))
    write_spec = adapter.tool_spec_for_definition(MCPToolDefinition(name="mutate"))

    assert read_spec is not None
    assert write_spec is not None
    assert read_spec.repeat_policy == "distinct_inputs"
    assert write_spec.repeat_policy == "once_per_run"


def test_mcp_proxy_registry_uses_the_same_repeat_policy_mapping() -> None:
    config = MCPToolAdapterConfig(
        server_name="server-sentinel",
        allowed_tools=["lookup", "mutate"],
        read_only_tools=["lookup"],
    )
    adapter = MCPToolAdapter(config, runner=cast(Any, object()))
    registry = ToolRegistry()
    registry.register(adapter.proxy_tool_for_definition(MCPToolDefinition(name="lookup")))
    registry.register(adapter.proxy_tool_for_definition(MCPToolDefinition(name="mutate")))

    assert registry.get_spec("mcp.server-sentinel.lookup").repeat_policy == "distinct_inputs"
    assert registry.get_spec("mcp.server-sentinel.mutate").repeat_policy == "once_per_run"
```

增加 decorated tool 的可配置投影用例，断言 `@tool(..., repeat_policy="distinct_inputs")` 注册后 Registry 返回相同策略。

- [ ] **Step 2: 运行定向测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py \
  -k "mcp or decorated"
```

Expected: MCP read spec/proxy 实际得到 `once_per_run`，decorator 因尚不接受 `repeat_policy` 而失败。

- [ ] **Step 3: 实现最小公共契约与 MCP 映射**

在 `tools/base.py` 给 Protocol 与基类加入类型化声明：

```python
from assistant_agent.tools.models import ToolRepeatPolicy

class Tool(Protocol):
    repeat_policy: ToolRepeatPolicy

class ToolBase:
    repeat_policy: ToolRepeatPolicy = "once_per_run"
```

在 `tools/decorators.py` 让 `DecoratedTool` 和 `tool()` 接受并保存同名参数。在 `mcp/adapter.py` 复用单个纯函数：

```python
def _repeat_policy_for_tool(config: MCPToolAdapterConfig, tool_name: str) -> ToolRepeatPolicy:
    return "distinct_inputs" if config.is_read_only(tool_name) else "once_per_run"
```

`MCPProxyTool.repeat_policy` 与 `tool_spec_for_definition(...).repeat_policy` 都调用该函数。

- [ ] **Step 4: 运行 Task 1 测试并确认 GREEN**

Run 同 Step 2。Expected: selected tests PASS。

---

### Task 2: 内置工具分类

**Files:**
- Modify: `tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/calendar_weather_contacts/tools.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/email_access/tools.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/local_file_access/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/watch_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/video_branch.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/python_execution/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/visual_image_search/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/fetch_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/search_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/website_guidance/tools.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/durable_task/tool.py`

**Interfaces:**
- Consumes: Task 1 的 `ToolBase.repeat_policy` 声明和 Registry 投影。
- Produces: 每个 concrete builtin Tool class 的显式策略；24 个 `distinct_inputs`、3 个 `once_per_run`。

- [ ] **Step 1: 写分类行为的失败测试**

参数化 concrete class，通过不执行构造副作用的 `tool_type.__new__(tool_type)` 注册真实类契约；断言 Registry 投影的字面期望值：

```python
@pytest.mark.parametrize(
    ("tool_type", "expected"),
    [
        (WeatherTool, "distinct_inputs"),
        (CalendarCreateTool, "distinct_inputs"),
        (VisualReminderManageTool, "distinct_inputs"),
        (PythonInterpreterTool, "distinct_inputs"),
        (WebPageExploreTool, "distinct_inputs"),
        (TaskPlanSubmitTool, "once_per_run"),
        (ImageGenerationTool, "once_per_run"),
        (ImageTo3DTool, "once_per_run"),
    ],
)
def test_builtin_registry_projects_the_approved_repeat_policy(tool_type: type[ToolBase], expected: str) -> None:
    registry = ToolRegistry()
    registry.register(tool_type.__new__(tool_type))
    assert registry.get_spec(tool_type.name).repeat_policy == expected
```

实际参数表必须列出设计规格中的全部 concrete builtin class，包括三个 media inspection class 与内部 `VideoUnderstandingBranch`；相同注册名的类在独立 Registry 中分别验证。

- [ ] **Step 2: 运行分类测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py \
  -k "approved_repeat_policy"
```

Expected: 尚未显式改为 `distinct_inputs` 的内置类失败；已有 shopping 与默认单次类通过。

- [ ] **Step 3: 为每个内置类显式声明策略**

在每个 concrete class 的 `category` 附近加入以下两种声明之一：

```python
repeat_policy = "distinct_inputs"
```

或：

```python
repeat_policy = "once_per_run"
```

严格按设计表分类；`LiveViewInspectTool` 与 `RealtimeVideoObserveTool` 也各自显式声明，不只依赖父类继承。对用户已修改的 `visual_memory_tool.py` 只插入单行，不改其余逻辑。

- [ ] **Step 4: 运行分类测试并确认 GREEN**

Run 同 Step 2。Expected: 全部参数 case PASS。

---

### Task 3: 通用相同输入去重与图片生成特判移除

**Files:**
- Modify: `tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py`
- Modify: `src/assistant_agent/runtime/loop_guard.py`
- Modify: `src/assistant_agent/runtime/assistant_loop_nodes.py`

**Interfaces:**
- Consumes: Registry 中的 `ToolSpec.repeat_policy` 和 `LoopGuard` 的规范化调用签名。
- Produces: 所有成功工具调用均记录调用签名；执行边界直接拒绝相同成功输入；图片生成只由 `once_per_run` 通用策略控制。

- [ ] **Step 1: 写不同输入与相同输入批量执行的失败测试**

增加一个真实可计数的 `category="write"`、`repeat_policy="distinct_inputs"` probe，并通过 `execute_requested_tool_node` 运行同一 Provider batch：

```python
def test_distinct_input_write_tool_blocks_identical_calls_at_execution_boundary() -> None:
    tool = _RepeatableWriteProbeTool()
    registry = ToolRegistry()
    registry.register(tool)
    state = _empty_state()

    executed = execute_requested_tool_node(
        _batch_graph_state(state, registry, inputs=["same", "same"])
    )

    assert tool.executed_queries == ["same"]
    assert executed["tool_observations"][-1]["error"]["code"] == "duplicate_complete_tool_call"
```

另加不同参数 batch `first -> second`，断言执行两次，以防把 `distinct_inputs` 错误收紧为单次策略。

- [ ] **Step 2: 写图片生成名称特判的失败测试**

注册一个名称为 `image_generation`、策略为 `once_per_run` 的成功 probe；在 metadata 中放入旧
`succeeded_terminal_tools` 状态，再对已有成功记录应用 decision guard：

```python
decision = _apply_decision_guards(...)
assert isinstance(decision, AssistantToolCall)
assert "tool_repeat_limit_reached" in decision.safety_notes
assert "duplicate_terminal_tool" not in decision.safety_notes
```

该测试在现状下返回 `AssistantTextOutput`，证明旧名称特判仍抢先于统一策略。

- [ ] **Step 3: 运行 Task 3 测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/tool-repeat-policy/test_tool_repeat_policy.py \
  -k "execution_boundary or image_generation_uses"
```

Expected: 相同 write batch 实际执行两次；图片生成命中 `duplicate_terminal_tool`。

- [ ] **Step 4: 用通用 guard 实现相同成功输入去重**

在 `_execute_single_requested_tool_node` 的 repeat-policy 检查后，直接根据
`LoopGuard.complete_call_already_seen(tool_name, tool_input)` 构造
`duplicate_complete_tool_call` rejection，确保未经 decision guard 的 native batch 也无法重复执行。

成功结果不再受 `category == "read"` 或 observation 完整度限制，统一记录签名：

```python
if result.success:
    LoopGuard(state.request.metadata).record_complete_tool_success(
        tool_name=tool_name,
        tool_input=tool_input,
    )
```

同步把 `LoopGuard.complete_call_already_seen()` 与 `record_complete_tool_success()` 的 docstring
从“read/complete observation”收敛为“成功调用”，但保留现有 metadata key 和错误码以兼容已有 trace。
保留失败输入与 non-recoverable guard 的既有行为。

- [ ] **Step 5: 删除图片生成专用终止工具路径**

从 `loop_guard.py` 删除 `IMAGE_GENERATION_TOOL_NAME` import、`terminal_tools`、
`record_terminal_tool_success()` 与 `terminal_tool_already_succeeded()`。从
`assistant_loop_nodes.py` 删除 decision 阶段的 `duplicate_terminal_tool` 分支及成功后的
`record_terminal_tool_success(tool_name)` 调用。

- [ ] **Step 6: 运行 Task 3 测试并确认 GREEN**

Run 同 Step 3。Expected: selected tests PASS。

- [ ] **Step 7: 运行整个 feature TDD 集合**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/tool-repeat-policy
```

Expected: 全部 PASS，无 warnings/errors。

---

### Task 4: 权威文档同步与最终验证

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Keep uncommitted: `docs/superpowers/specs/2026-08-06-tool-repeat-policy-classification-design.md`
- Keep uncommitted: `docs/superpowers/plans/2026-08-06-tool-repeat-policy-classification.md`

**Interfaces:**
- Consumes: Tasks 1-3 已通过测试的实际行为。
- Produces: 与源码一致的当前架构说明和可复核的最终 diff。

- [ ] **Step 1: 窄幅更新权威文档**

在 `repeat_policy` 与 MCP 小节补充：

```text
内置 Tool 必须显式声明 repeat_policy；ToolBase 的 once_per_run 只作为旧式/外部工具的保守回退。
MCP read_only_tools 映射为 distinct_inputs，未可信声明为只读的 MCP 工具映射为 once_per_run。
相同成功输入对所有 category 去重；Runtime 不再维护按工具名定义的终止工具重复策略。
```

只修改相关段落，保留该文件中用户的其他未提交改动。

- [ ] **Step 2: 运行格式和静态完整性检查**

Run:

```bash
git diff --check
rg -n "terminal_tool_already_succeeded|record_terminal_tool_success|duplicate_terminal_tool" \
  src/assistant_agent/runtime
```

Expected: `git diff --check` exit 0；第二条 `rg` 无匹配并以 exit 1 结束。

- [ ] **Step 3: 重新运行整个 feature TDD 集合**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/tool-repeat-policy
```

Expected: 全部 PASS，无真实 Provider 调用。

- [ ] **Step 4: 审核 scope 并决定提交**

运行 `git status --short` 与逐文件 `git diff`。若本任务所有源码/测试 hunk 能与用户既有改动安全隔离，
仅提交本任务源码、测试和权威文档；spec/plan 保持未跟踪。若重叠文件无法保证只提交本任务 hunk，
不创建 commit，并在最终报告中明确说明。

- [ ] **Step 5: 按项目格式汇报**

最终报告必须包含：完成内容、实际验证命令与结果、未完成/限制，以及：

```text
Core invariant: unchanged.
Tests: updated tests/tdd/tool-repeat-policy for temporary RED/GREEN; user may delete the directory manually.
```
