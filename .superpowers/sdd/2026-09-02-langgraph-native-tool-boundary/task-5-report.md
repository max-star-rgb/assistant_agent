# Task 5 报告

## RED

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-tools/test_media_tools.py
```

结果：失败，`ModuleNotFoundError: assistant_agent.media.video.understanding_service`。

## GREEN 与验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-tools/test_media_tools.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/media/video/understanding_service.py src/assistant_agent/media/vision/models.py src/assistant_agent/tools/plugins/builtin/media_inspection evals/system/realtime_visual_target_window/runner.py
PYTHONPATH=src MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -c 'from assistant_agent.media.video.understanding_service import VideoUnderstandingService; from evals.system.realtime_visual_target_window.runner import dry_run_report; print(VideoUnderstandingService.__name__, dry_run_report(frame_dir=None, allow_real_provider=False)["network_called"])'
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_system_realtime_visual_target_window_eval.py --dry-run
```

结果：pytest `5 passed`；compile/import 通过；dry-run 为 mock，`network_called=false`。

## 移动与消费者

- `tools/plugins/builtin/media_inspection/video_branch.py` 已移至 `media/video/understanding_service.py`，公开类为 `VideoUnderstandingService`。
- 明确消费者已更新：`uploaded_tool.py`、`live_tool.py`、`evals/system/realtime_visual_target_window/runner.py`。
- `visual_memory_tool.py`、`visual_reminder_tool.py` 已直接投影原生 content/artifact；四个媒体 Tool 均启用窄 `bounded_validation_errors`，覆盖 ToolNode schema 敏感 sentinel 不回显。

## 风险

- 未运行真实 Provider；仅 mock/offline 验证。
- live exact-target 仍保持原有最多 4 秒等待和视觉后台并行流水线；本任务未更改 selector、observer 或语义 store。

## Fix round 1

RED：扩展真实 ToolNode 的四个普通异常 sentinel 覆盖后，`uploaded_media_inspect` 将
`api_key=uploaded-tool-sentinel` 原样投影到 error ToolMessage。

GREEN：`uploaded_media_inspect`、`live_view_inspect`、`visual_memory_search` 和
`visual_reminder_manage` 的完整 handler 主体均在顶层保留 `ToolException`、将普通
`Exception` 交给 `native_tool_exception`；不捕获取消/中断使用的 `BaseException`。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langgraph-native-tools/test_media_tools.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q src/assistant_agent/tools/plugins/builtin/media_inspection tests/tdd/langgraph-native-tools/test_media_tools.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_system_realtime_visual_target_window_eval.py --dry-run
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check src/assistant_agent/tools/plugins/builtin/media_inspection/live_tool.py src/assistant_agent/tools/plugins/builtin/media_inspection/uploaded_tool.py src/assistant_agent/tools/plugins/builtin/media_inspection/visual_memory_tool.py src/assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py tests/tdd/langgraph-native-tools/test_media_tools.py
```

结果：pytest `6 passed`；compile 与 ruff 通过；dry-run 保持 mock，`network_called=false`。
