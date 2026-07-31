# Runtime feature checks

- Scope: 旧 Runtime safety net、durable SQLite、proactive wake、长运行复用与 Provider streaming 实现。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/runtime-features/checks_*.py`
- Side effects and gates: 只使用 scripted/mock Provider、in-memory 或临时 SQLite；不读取真实 `.env`，不调用真实 Provider。
- Delete when: 核心 Runtime/Durable 不变量、正式 system runner 或生产恢复证据已稳定覆盖相关事实。
- Promote when: 真实 Provider streaming 或恢复链路进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。
