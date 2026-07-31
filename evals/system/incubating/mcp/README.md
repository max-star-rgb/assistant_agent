# MCP checks

- Scope: MCP 配置样例、Runtime 注册与 SDK 环境适配。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/mcp/checks_*.py`
- Side effects and gates: 只允许 mock/local MCP transport；不读取真实 `.env`，不连接真实 MCP server。
- Delete when: 正式 MCP system runner 和生产握手/调用证据已稳定覆盖这些适配事实。
- Promote when: 真实 MCP 连通性进入 real mode、完整配置及 operator `--allow-*` 门禁的正式 system eval。
