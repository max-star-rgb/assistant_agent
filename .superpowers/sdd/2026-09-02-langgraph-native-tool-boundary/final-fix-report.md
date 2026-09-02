# LangGraph 原生 Tool 边界最终修复报告

基线：`430db720`。本波次只修复最终审查列出的 Tool 安全边界、视觉提醒错误语义、测试命名、authority owner 和已确认的无效透传；未提交未跟踪 implementation plan。

## RED / GREEN

先新增真实 `ToolNode` 参数化回归，再修改生产代码：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/langgraph-native-tools/test_compatibility_prune.py::test_business_tool_schema_errors_do_not_echo_call_kwargs \
  tests/tdd/langgraph-native-tools/test_media_tools.py::test_visual_reminder_create_preserves_safe_specific_failures
```

修复前 RED：`13 failed, 8 passed`。其中恰好 10/17 个业务 Tool 回显各自独立 schema sentinel；视觉提醒的 connection 分支通过，joint embedding、text embedding failed、embedding incompatible 三个分支均错误返回 `visual reminder connection is unavailable`。

修复后 GREEN：`21 passed in 1.83s`。完整 native-tool TDD 与 TOOL-001 定向集合随后为 `63 passed in 5.17s`。

## Finding 处理

1. `configure_builtin_tool` 删除 `bounded_validation_errors` opt-in，项目 builtin 默认安装不含原始 input 的 `handle_validation_error`。17 个业务 factory 均由真实默认 `ToolNode` 验证独立 sentinel 不回显；原先 7 个调用点的无意义 opt-in 参数已删除。Deep Agents 与 MCP Tool 不调用该 helper；Tool Profile 改为只设置原有 metadata，未改变其 validation 行为。
2. `visual_reminder_manage` 不再把所有 create unavailable 状态重写为 connection unavailable。connection 缺失、joint embedding unavailable、text embedding unavailable、embedding incompatible 恢复迁移前的具体安全 `ToolException`；普通未知异常仍经 `native_tool_exception` 清洗，ToolNode 结果包含 `[redacted]` 且不含 sentinel。
3. TOOL-001 的 unknown-failure probe 更名为 `test_production_unknown_failure_handler_sanitizes_before_toolnode`，并诚实执行生产形态 `unknown exception -> native_tool_exception -> ToolNode`。
4. Tool authority 的 17 项清单补入 `file_read`、移除 `git`；`git` 仍是直接 native Tool，但不属于本次 17 个业务迁移清单。
5. `media/vision/models.py` 唯一归 `visual-perception`；四个 `media_inspection` Tool adapter 唯一归 `tool-calling`，消除了 source glob 重叠。validator 的当前 `review_required` 为 documentation-index、test-policy、tool-calling、visual-perception，已逐项人工复核；完整分支范围额外包含既有 system-eval，已按其 authority 用两个 mock dry-run 复核。
6. 删除只复制 `user_id/session_id/metadata` 的 `_InspectionContext`，`VideoUnderstandingService` 直接使用 `VideoUnderstandingRequest`。删除 contacts/email/local-file 领域 helper 中立即 `del` 的无效 runtime facts 参数与传递；三个 handler 的 `ToolRuntime` 注入及模型可见 schema 保持不变。
7. 设计 spec 第 3 行 trailing whitespace 已删除。

## 最终验证（mock/offline）

```text
tests/core: 81 passed in 7.71s
三个联合 TDD 目录: 111 passed in 6.34s
realtime visual target dry-run: provider_mode=mock, network_called=false
multimodal embedding dry-run: local_model_loaded=false
authority validator: valid=true, errors=[]
ruff check src tests/core tests/tdd/langgraph-native-tools scripts: All checks passed
compileall -q src scripts: exit 0
git diff --check f71253fe: exit 0（提交前工作树范围）
git diff --check f71253fe..HEAD: exit 0（提交后精确范围）
```

全仓生产源码搜索确认 `bounded_validation_errors`、`_InspectionContext`、`invoke_native_tool`、`native_tool_response`、`ToolContext` 和 `tool_context(` 均无残留；contacts/email/local-file 的无效 facts 传递无残留。未调用真实 Provider。

## 唯一剩余验收

未在隔离 worktree 启动或连接第二套 `8089`。合并到主工作区后，必须仅使用 PyCharm 管理的唯一现有 dev server 验证 hot reload、health 与三个生产 Graph；这是集成后的唯一剩余验收，不属于本修复波次可安全执行的项目。

Core invariant: TOOL-001 implementation updated; structured contract unchanged.

Tests: updated `tests/core/contract/test_tool_contract.py` and `tests/tdd/langgraph-native-tools`；后者为临时 RED/GREEN，用户可在功能稳定后手动删除。
