# Provider observability checks

- Scope: Langfuse/OTel、trace 内容、span timing、事件发布和 context report 的旧实现检查。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/provider-observability/checks_*.py`
- Side effects and gates: 只使用内存 exporter、mock Provider 和本地状态；不读取真实 `.env`，不上传 trace。
- Delete when: 正式 observability harness 和生产 trace 证据已稳定覆盖相关事实。
- Promote when: 真实 trace/exporter 连通性进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。
