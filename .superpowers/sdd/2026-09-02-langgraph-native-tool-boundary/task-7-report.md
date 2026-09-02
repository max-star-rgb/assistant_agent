# Task 7 报告：Authority 收口与最终验证

## 文档与路由

- `tool-calling-architecture.md` 现声明 17 个内建业务 Tool 直接使用 `ToolRuntime`、`content/artifact` 与 `ToolException`；旧 `ToolContext` / `invoke_native_tool` / `ToolResult` 生产执行链已删除。
- `runtime-event-stream-architecture.md` 记录 `ToolResult` 仅保留给 `runtime/state.py` 历史兼容记录，不属于 native Tool 边界。
- `visual-perception-architecture.md` 指向迁移后的 `media/video/understanding_service.py`，并将可信视觉边界说明为 `ToolRuntime` 注入。
- `durable-task-architecture.md` 明确 Tool adapter 仅经 service 访问 durable task，状态机、lease 与 checkpoint 仍由 durable domain 所有。
- `authority.toml` 移除了媒体协议域对 `src/assistant_agent/media/video/**` 的 source glob；该目录仅由 visual-perception domain 覆盖。

## 验证（mock/offline）

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
# {"errors": [], "review_required": ["documentation-index"], "schema_version": 1, "valid": true}

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src tests/core tests/tdd/langgraph-native-tools scripts
# All checks passed!

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src scripts
git diff --check
# exit 0

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core
# 81 passed in 7.14s

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools tests/tdd/image-generation-studio-link
# 39 passed in 2.54s

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_realtime_visual_target_window_eval.py --dry-run
# provider_mode=mock; network_called=false

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_multimodal_embedding_eval.py --dry-run
# local_model_loaded=false; no real Provider call
```

`documentation-index` 是本次 manifest 修改导致的唯一 `review_required`；已复核其 `docs/authority.toml` 路由，错误为空。

搜索确认 `src/assistant_agent/tools` 与 `src/assistant_agent/native_agent` 不再含 `ToolContext` 或
`invoke_native_tool`。`ToolResult` 仅出现在 `tools/models.py` 定义及 `runtime/state.py` 的历史兼容记录。

## 已知失败

下列简报指定命令已实际运行，但以一个既有、与本次纯文档变更无关的断言失败结束：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools \
  tests/tdd/image-generation-studio-link \
  tests/tdd/unified-assistant-agent
# 89 passed, 1 failed
```

失败用例是 `test_main_and_worker_use_the_configured_summarization_budget`。它查找
`langchain.agents.middleware.SummarizationMiddleware`，而当前 runtime 已装配
`RuntimeConfigurableSummarizationMiddleware`（Deep Agents summarizer）；`StopIteration` 可单独稳定复现。
该 runtime/TDD 断言不在 Task 7 文档收口范围，未修改。其余两个指定 TDD 目录通过。

未启动第二套 `8089` 服务；hot reload 依计划延后至合并/集成主 checkout 后，用唯一现有 dev server 验证。

Core invariant: TOOL-001 implementation updated; structured contract unchanged.
Tests: added/updated tests/tdd/langgraph-native-tools for temporary RED/GREEN; user may delete the directory manually.
Provider: mock/offline only; no real Provider called.
