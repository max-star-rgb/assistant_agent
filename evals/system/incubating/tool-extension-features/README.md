# Tool extension feature checks

- Scope: 旧 Tool governance/observation、failure recovery、plugin assembly 与 plugin runtime。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/tool-extension-features/checks_*.py`
- Side effects and gates: 只使用通用 fake/local Tool 与受控 registry；不读取真实 `.env`，不调用真实外部 Tool。
- Delete when: 核心 Tool governance/extension 不变量和稳定 plugin 生产证据已覆盖相关事实。
- Promote when: 真实 Tool/Plugin 连通性进入 real mode、完整配置及 operator `--allow-real-tools` 门禁的正式 system eval。
