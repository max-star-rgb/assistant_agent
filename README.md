# assistant_agent

`assistant_agent` is a local-first multimodal autonomous tool-calling Agent. It uses a LangGraph/ReAct assistant loop, governed tool execution, provider adapters, memory services, API/demo/eval surfaces, and optional realtime Gateway entry layers.

## Start Here

Core project docs:

- Gateway and realtime lifecycle: [docs/gateway-architecture.md](docs/gateway-architecture.md)
- Runtime and provider event streaming: [docs/runtime-event-stream-architecture.md](docs/runtime-event-stream-architecture.md)
- Tool calling governance: [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md)
- Observability and trace harness: [docs/observability-harness.md](docs/observability-harness.md)
- Local memory service architecture: [docs/memory-service-architecture.md](docs/memory-service-architecture.md)
- External Memory Service interface: [docs/memory_server_api_spec.md](docs/memory_server_api_spec.md)
- Context engineering status: [docs/CONTEXT_ENGINEERING_STATUS.md](docs/CONTEXT_ENGINEERING_STATUS.md)
- Multi-agent routing: [docs/agent-communication-routing.md](docs/agent-communication-routing.md)
- Media-Agent WebSocket contract: [docs/media-agent-service-websocket.md](docs/media-agent-service-websocket.md)
- Test scopes and markers: [tests/README.md](tests/README.md)

## Local Environment

The Python package is `assistant_agent` under `src/assistant_agent/`. The local conda environment remains `hello_agent`.

Provider profiles and external-provider configuration are documented in [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md).

可信 Python Tool 插件可通过 `MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES` 显式配置，重启后生效。可用
`python -m assistant_agent.tools.cli plugins` 查看只读装配报告；该机制会执行所配置 module 的进程内代码，
不是不可信代码沙箱。具体协议和治理边界见 Tool calling 文档。

Basic checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Additional deterministic offline checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
