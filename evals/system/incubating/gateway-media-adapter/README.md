# Gateway media adapter checks

- Scope: Gateway connection lease、turn mode、interrupt 与 realtime delivery/revision adapter。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/gateway-media-adapter/checks_*.py`
- Side effects and gates: 使用 fake WebSocket、mock media/runtime 和本地状态；不读取真实 `.env`，不连接真实媒体服务。
- Delete when: 核心 Gateway 不变量、正式媒体 system runner 和真实通话 trace 已稳定覆盖对应事实。
- Promote when: 真实媒体或电话链路进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。
