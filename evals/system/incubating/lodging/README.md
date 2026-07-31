# Lodging checks

- Scope: lodging Tool、FlyAI adapter 与 hotel price watch 专项行为。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/lodging/checks_*.py`
- Side effects and gates: 使用 mock/local adapter 和临时状态；不读取真实 `.env`，不调用住宿 Provider。
- Delete when: 住宿正式 system runner、Agent Task Experiment 或生产监控已稳定覆盖对应事实。
- Promote when: 真实住宿 Provider 连通性进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。
