# Task 6：旧 Tool 兼容执行链裁剪

## RED / GREEN

- RED：新增 `tests/tdd/langgraph-native-tools/test_compatibility_prune.py` 后，
  `test_legacy_execution_functions_are_not_a_production_tool_boundary` 因
  `native_boundary.invoke_native_tool` / `native_tool_response` 仍存在而失败。
- GREEN：删除这两个函数、`tools/runtime.py` 与 `tools/tool_lifecycle.py`，并裁剪
  `tools/observation.py` 后，临时测试 `3 passed`；简报指定集合 `47 passed`。

## 删除与收口

- 删除 `invoke_native_tool`、`native_tool_response` 及它们的 `ToolResult` 投影链。
- 删除无调用者的 `ToolContext`、`tool_context`、`latest_human_request`
  （`src/assistant_agent/tools/runtime.py`）。
- 删除无调用者的 `build_tool_lifecycle_summary`
  （`src/assistant_agent/tools/tool_lifecycle.py`）。
- `observation.py` 只保留 `prompt_observation_payload` 和
  `native_tool_observation_payload` 的最小投影；移除 ToolResult-based 观察模型与 helper。
- TOOL-001 probe 直接返回原生 `(content, artifact)` 或抛出 `ToolException`，保留
  成功 ToolMessage、预期失败、未知错误清洗与 read retry 契约。

## ToolResult 保留理由

`ToolResult` 保留在 `src/assistant_agent/tools/models.py`，但不再是生产 Tool 边界类型。
唯一实际类型消费者是旧 runtime/state 切面：

- `src/assistant_agent/runtime/state.py:67` 的 `AgentState.tool_results` 历史列表；
- `src/assistant_agent/runtime/state.py:130` 的 `complete_tool_call`；
- `src/assistant_agent/runtime/state.py:147` 的 `fail_tool_call`。

可观察性模块仅保留文档字符串中的 “ToolResult” 名称，没有类型 import 或构造。该类型登记为后续切面四候选，未在本任务跨越 runtime/state authority 继续删除。

## 验证（全部 mock/offline）

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools/test_compatibility_prune.py
# 3 passed

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py \
  tests/tdd/langgraph-native-tools
# 47 passed

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools/native_boundary.py \
  src/assistant_agent/tools/observation.py \
  tests/core/contract/test_tool_contract.py \
  tests/core/support.py \
  tests/tdd/langgraph-native-tools/test_compatibility_prune.py
# All checks passed

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/tools \
  tests/core/contract/test_tool_contract.py \
  tests/core/support.py \
  tests/tdd/langgraph-native-tools/test_compatibility_prune.py

git diff --check
```

Core invariant: TOOL-001 changed because core probe no longer relies on the removed project compatibility execution chain.
Tests: updated existing `tests/core/contract/test_tool_contract.py`; added temporary RED/GREEN probe under `tests/tdd/langgraph-native-tools`.
