# assistant_agent

本项目仍在快速演进中，README 暂时只保留占位入口。

当前开发规则、运行边界、文档路由和 agent 工作入口以 [AGENTS.md](AGENTS.md) 为准。项目稳定后再重写面向人的 README。

当前项目展示名、发行名和 Python 包名均为 `assistant_agent`，包目录为 `src/assistant_agent/`；本地 conda 环境仍为 `hello_agent`。

本地运行默认使用 mock/local/offline Provider；不会因为本地存在 key 自动启用真实调用。API key 只用于显式 opt-in 的真实 Provider smoke/pilot。

本地调试接口包括 `/health`、`GET /runs/{run_id}`、`GET /traces/{trace_id}` 和 `GET /runs/{run_id}/tool-calls`，用于检查 `run_id`、`trace_id`、`tool_calls`、provider error 与 budget 等运行信息。

常用本地验证命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
```

专项说明文档保留在 `docs/`：

- `docs/CONTEXT_ENGINEERING_STATUS.md`
- `docs/context-engineering-walkthrough.md`
- `docs/memory-service-architecture.md`
- `docs/memory-module-walkthrough.md`
- `docs/tool-calling-architecture.md`
- `docs/agent-communication-routing.md`
- `docs/agent-collaboration-walkthrough.md`
