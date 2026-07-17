# Tool 级真实 Provider 手动 Smoke

本目录只放开发者手动触发的 tool-level 真实 Provider 检查，不属于普通自动化测试路径。它们用于在
本机 `.env` 已配置真实 provider 时，直接观察：

- provider 配置诊断；
- provider raw 输出（adapter 暴露时）；
- 完整 `ToolResult`；
- 筛选后的 LLM-facing tool observation；
- tool 执行成功、语义成功、领域发布资格等分层结果。

## 运行方式

推荐直接运行单个文件或单个 test node，PyCharm 的单文件运行也属于这个模式：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_qwen_realtime_vision_attached_image_provider.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_vision_understanding_attached_image_provider_smoke.py
```

如果要从更宽的选择范围里强制运行某个真实 provider smoke，必须显式设置对应 opt-in：

```bash
RUN_REAL_VLM_IMAGE_TEST=1 /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_qwen_realtime_vision_attached_image_provider.py
RUN_REAL_VISION_IMAGE_TOOL_TEST=1 /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_vision_understanding_attached_image_provider_smoke.py
```

不要把本目录加入 `tests/scopes/**`、`critical` 或普通 `scripts/run_scoped_tests.py` 开发路径。

## 命名约定

- `test_<tool_name>_<provider_or_configured>_<asset>_provider_smoke.py`
- provider 专属链路写 provider 名，例如 `qwen_realtime`。
- 可复用多 provider 的链路使用 `configured_provider` 或省略 provider 名，并在测试内读取
  `MULTIMODAL_AGENT_*_PROVIDER`。
- 通用图片准备、分层成功诊断和 LLM-facing observation 打印放在 `manual_tool_smoke.py`。

## 现有文件

- `test_qwen_realtime_vision_attached_image_provider.py`：验证 `video_understanding` 工具到
  Qwen realtime WebSocket VLM 的真实链路，包含 snapshot publishability 判断。
- `test_vision_understanding_attached_image_provider_smoke.py`：验证 `vision_understanding`
  图片工具到当前配置真实 vision provider 的链路，适合作为未来 OpenAI/Qwen/Ark 等 provider
  的共同 smoke 入口。
