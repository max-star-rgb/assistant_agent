# Shopping checks

- Scope: shopping 搜索、候选可比性、结果 outcome、恢复与 plugin 装配。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/shopping/checks_*.py`
- Side effects and gates: 只使用 mock/local shopping provider；不读取真实 `.env`，不请求好单库或其他外部服务。
- Delete when: 正式 shopping system runner 与对应 Agent Task Experiment 已稳定覆盖搜索和决策事实。
- Promote when: 真实好单库连通性进入带 `--allow-real-tools` 的正式 system runner；模型选择质量进入 Agent eval。
