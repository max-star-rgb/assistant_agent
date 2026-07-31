# Workspace tool checks

- Scope: email、calendar、contacts、local file 与 weather profile 等具体 workspace 节点。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/workspace-tools/checks_*.py`
- Side effects and gates: 只使用 mock/local adapter 与临时目录；不读取真实 `.env`，不访问真实邮箱、日历、联系人或天气服务。
- Delete when: 各节点正式 system runner、Agent Task Experiment 或生产证据已稳定覆盖相关事实。
- Promote when: 真实 workspace Tool 连通性进入 real mode、完整配置及 operator `--allow-real-tools` 门禁的正式 system eval。
