# Agent eval infrastructure checks

- Scope: Agent Task、Mission、Langfuse Dataset/Experiment 与远程控制面的旧确定性实现检查。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/agent-eval-infrastructure/checks_*.py`
- Side effects and gates: 只允许受控 fake、mock 和本地状态；不读取真实 `.env`，不发布 Dataset，不运行真实 Judge 或 Provider。
- Delete when: 对应 Task/Mission 的正式校准、Experiment 和控制面证据已稳定覆盖这些事实。
- Promote when: 真实 Agent 行为进入 `evals/agent`；真实控制面连通性进入显式门禁的正式 system runner。
