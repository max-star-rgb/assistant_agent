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
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_realtime_vision_attached_image_provider.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_image_generation_provider_smoke.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_shopping_search_provider_smoke.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_web_search_provider_smoke.py
```

如果要从更宽的选择范围里强制运行某个真实 provider smoke，必须显式设置对应 opt-in：

```bash
RUN_REAL_VLM_IMAGE_TEST=1 /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_realtime_vision_attached_image_provider.py
RUN_REAL_IMAGE_GENERATION_TOOL_TEST=1 /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_image_generation_provider_smoke.py
RUN_REAL_SHOPPING_TOOL_TEST=1 /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_shopping_search_provider_smoke.py
RUN_REAL_WEB_SEARCH_TOOL_TEST=1 /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q -s tests/integration/tools/test_web_search_provider_smoke.py
```

不要把本目录加入 `tests/scopes/**`、`critical` 或普通 `scripts/run_scoped_tests.py` 开发路径。

## 命名约定

- `test_<tool_name>_<provider_or_configured>_<asset>_provider_smoke.py`
- provider 专属链路写 provider 名，例如 `qwen_realtime`。
- 可复用多 provider 的链路使用 `configured_provider` 或省略 provider 名，并在测试内读取
  `MULTIMODAL_AGENT_*_PROVIDER`。
- 每个文件保持可独立运行；除统一度量 helper `tests.tool_smoke_metrics` 外，不要依赖普通自动化
  测试里的 fixture 或外部 helper。

## 统一度量

每个 tool smoke 必须打印 `TOOL SMOKE METRICS` JSON 区块，schema 为
`tool_smoke_metrics_v1`。其中：

- `tool_elapsed_ms` 是测试进程用 `perf_counter` 包住 `tool.run(...)` 得到的总耗时，是人工判断本次
  tool 调用耗时的首选字段。
- `reported_latency_ms` 保存工具、contract、data 或 provider diagnostics 自己上报的耗时分项；这些
  字段可能为空，也可能只覆盖 provider 观察阶段，不能替代 `tool_elapsed_ms`。
- `result` 保存本次 tool 是否成功、contract 状态、`output_ref` 和是否存在错误，便于和耗时一起看。

## 现有文件

- `test_realtime_vision_attached_image_provider.py`：验证 `video_understanding` 工具到
  Qwen realtime WebSocket VLM 的真实链路，包含 snapshot publishability 判断。
- `test_image_generation_provider_smoke.py`：验证 `image_generation` 工具到当前配置真实图片生成
  provider 的链路，默认使用 Qwen，支持切换到已实现的 Ark。
- `test_shopping_search_provider_smoke.py`：验证 `shopping_search` 工具到好单库搜索与比价链路，
  默认要求 `MULTIMODAL_AGENT_PRODUCT_PROVIDER=haodanku` 和
  `MULTIMODAL_AGENT_PRICE_PROVIDER=haodanku`。
- `test_web_search_provider_smoke.py`：验证 `web_search` 工具到 HTTP 联网搜索 provider 的链路，
  默认要求 `MULTIMODAL_AGENT_SEARCH_PROVIDER=http`。
