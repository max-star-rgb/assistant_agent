# Task 4 报告：生成与 durable task Tool 原生化

## TDD

RED：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools/test_effect_tools.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

结果：`1 failed, 4 passed`。失败点为三个待迁移模块仍导入 `ToolResult` / `invoke_native_tool`；既有图片输出测试保持基线通过。

GREEN：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools/test_effect_tools.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

结果：`5 passed`。

额外检查：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/plugins/builtin/image_generation/tool.py \
  src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py \
  src/assistant_agent/tools/plugins/builtin/lodging/watch_tool.py \
  tests/tdd/langgraph-native-tools/test_effect_tools.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

结果：`All checks passed!`；并以 `rg` 确认三个迁移模块没有 `ToolResult` 或 `invoke_native_tool` 导入，`git diff --check` 通过。

## 改动

- `image_generation` 直接使用 `ToolRuntime[AssistantRunContext]`、原生 content/artifact 投影和安全 `ToolException`，保留受管图片 artifact、文本与标准 image content block。
- `image_to_3d` 直接返回异步 job handoff；输入、Provider 与失败状态由安全 `ToolException` 投影。
- `hotel_price_watch_create` 直接通过现有 `HotelPriceWatchService` / `DurableTaskService` 创建 task，未复制 durable 状态机。
- 新增临时 effect TDD，覆盖真实 `ToolNode` schema 隐藏、原生图片内容、3D/job 与 hotel/task handoff、calendar idempotency 及兼容依赖移除。

## 风险与遗留

- 全部验证为 mock/offline，未调用真实 Provider。
- 未改变 `interrupt_on`、`require_tool_approval` 或 durable 状态机；它们继续由既有 composition/service 所有。

Core invariant: unchanged.

Tests: added/updated `tests/tdd/langgraph-native-tools` and `tests/tdd/image-generation-studio-link` for temporary RED/GREEN; user may delete the directories manually.

## Fix round 1：ValidationError 输入清洗与 calendar effect

RED：

```bash
PYTHONPATH=src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools/test_effect_tools.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

结果：`1 failed, 5 passed`。真实 ToolNode 的 `image_generation` / `hotel_price_watch_create` 内部 Pydantic `ValidationError` 错误含有 `input_value`；3D 同类错误路径也纳入回归测试。

GREEN：

```bash
PYTHONPATH=src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools/test_effect_tools.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

结果：`6 passed`。

额外检查：

```bash
PYTHONPATH=src MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/native_boundary.py \
  src/assistant_agent/tools/plugins/builtin/image_generation/tool.py \
  src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py \
  src/assistant_agent/tools/plugins/builtin/lodging/watch_tool.py \
  tests/tdd/langgraph-native-tools/test_effect_tools.py \
  tests/tdd/image-generation-studio-link/test_image_generation_output.py
```

结果：`All checks passed!`，`git diff --check` 通过。

修复：`native_tool_exception` 对 `ValidationError` 复用既有无 input 的 `_validation_error_message`；三个 effect handler 显式传入 Tool 名。calendar 测试保留 adapter 实例，验证只创建一次且实际收到 `native:thread:run:call-calendar_create`。
