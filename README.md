# assistant_agent

`assistant_agent` is a local-first multimodal autonomous tool-calling Agent. It uses a LangGraph/ReAct assistant loop, governed tool execution, provider adapters, memory services, API/demo/eval surfaces, and optional realtime Gateway entry layers.

## Start Here

Core project docs:

- Gateway and realtime lifecycle: [docs/gateway-architecture.md](docs/gateway-architecture.md)
- Runtime and provider event streaming: [docs/runtime-event-stream-architecture.md](docs/runtime-event-stream-architecture.md)
- Tool calling governance: [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md)
- Observability architecture and trace contract: [docs/observability-harness.md](docs/observability-harness.md)
- Real-run diagnosis runbook: [docs/observability-diagnosis-runbook.md](docs/observability-diagnosis-runbook.md)
- Website guidance（Qwen 候选 URL、Playwright 只读浏览与安全边界）: [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md)
- Memory Plugin architecture（排他 active Plugin，默认 Mem0）: [docs/memory-service-architecture.md](docs/memory-service-architecture.md)
- 默认 Mem0 Plugin 的私有 HTTP adapter 子集: [docs/memory_server_api_spec.md](docs/memory_server_api_spec.md)
- Context engineering architecture: [docs/context_engineering_status.md](docs/context_engineering_status.md)
- Multi-agent routing: [docs/agent-communication-routing.md](docs/agent-communication-routing.md)
- Media-Agent WebSocket contract: [docs/media-agent-service-websocket.md](docs/media-agent-service-websocket.md)
- 统一 SigLIP2 image/text embedding、短期视觉回忆与历史找物: [docs/multimodal-embedding-architecture.md](docs/multimodal-embedding-architecture.md)
- Core pytest、临时 TDD 与 incubating 边界: [tests/README.md](tests/README.md)
- System/Agent eval 与 incubating 运行规则: [evals/README.md](evals/README.md)

## Local Environment

The Python package is `assistant_agent` under `src/assistant_agent/`. The local conda environment remains `hello_agent`.

Provider profiles and external-provider configuration are documented in [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md).

## Package Layout

`src/assistant_agent/` 按能力所有权组织，不保留通用 `services`、全局 `schemas` 或独立 `realtime`
收纳层：

| package | responsibility |
| --- | --- |
| `runtime/` | assistant loop、运行状态、Provider stream、执行生命周期和应用编排 |
| `context/` | Context 构建、预算、压缩、渲染和 Tool catalog |
| `skills/` | Skill 加载、召回、校验、目录、执行与持久化 |
| `tools/` | Tool 契约、Registry、治理边界和 Plugin |
| `gateway/` | session/run/cancel/reconnect、Runtime adapter、事件映射和交付 |
| `media/` | 音频边缘适配、视频摄取/观察、统一 image/text embedding 及视觉 adapter |
| `automation/` | durable task、proactive wake 和通知 |
| `multi_agent/` | Agent routing、delegation、transport 和 A2A |
| `observability/` | trace、日志、metrics、OpenTelemetry 和 Langfuse |
| `improvement/` | 离线改进证据、提案、评估和报告 |
| `providers/` | 跨入口共享的 Provider 配置、错误治理和 adapter |
| `memory/` | 排他 Memory Plugin Host、session/run freeze、ingestion、受管媒体和默认 Mem0 Plugin |
| `api/` | FastAPI HTTP/WebSocket 薄入口 |
| `mcp/` | MCP 配置、client、registration 和 server adapter |
| `config/` | 进程配置装配 |

领域模型和稳定协议由所属 package 就近维护，例如 `tools/models.py`、
`context/models.py` 和 `multi_agent/a2a_protocol.py`。

可信 Python Tool 插件可通过 `MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES` 显式配置，重启后生效。可用
`python -m assistant_agent.tools.cli plugins` 查看只读装配报告；该机制会执行所配置 module 的进程内代码，
不是不可信代码沙箱。具体协议和治理边界见 Tool calling 文档。

可信 Python Memory Plugin 使用独立的 `assistant_memory_plugin_v1` 和排他 `memory` slot，通过
`MULTIMODAL_AGENT_MEMORY_PLUGIN_CONFIG_PATH` 显式配置，重启后生效。可用
`python -m assistant_agent.memory.cli plugins` 查看只读、脱敏的装配报告；默认内置实现为 Mem0，
`Mem0Client` 仅是该 Plugin 的私有 adapter。Memory Plugin 不注册 Tool，也不直接修改 Prompt 或
`AgentState`。具体生命周期、mock/real 边界和 CLI schema 见 Memory Plugin 架构文档。

Basic check（只运行稳定的 `tests/core` 核心框架安全网）：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

功能实现期间的临时 RED/GREEN 放在 `tests/tdd/*/` 下独立的 feature 目录，必须显式运行，可由用户
手动整目录删除。
有风险证据的节点专项检查放在 `evals/system/incubating/<feature>/`，也不进入默认 pytest。具体命令和
准入规则见 [tests/README.md](tests/README.md)。

Additional deterministic offline checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```
