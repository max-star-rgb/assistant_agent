# assistant_agent

`assistant_agent` 是本地优先的多模态工具调用 Agent。生产 Assistant Graph 运行在 LangGraph Agent Server；媒体服务通过兼容的 custom route 接入。

## Start Here

Core project docs:

- Agent Server deployment and native resource lifecycle: [docs/agent-server-architecture.md](docs/agent-server-architecture.md)
- Runtime and provider event streaming: [docs/runtime-event-stream-architecture.md](docs/runtime-event-stream-architecture.md)
- Tool calling governance: [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md)
- Observability architecture and trace contract: [docs/observability-harness.md](docs/observability-harness.md)
- Real-run diagnosis runbook: [docs/observability-diagnosis-runbook.md](docs/observability-diagnosis-runbook.md)
- Website guidance（Qwen 候选 URL、Playwright 只读浏览与安全边界）: [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md)
- LangGraph 原生长期记忆（固定节点、冻结快照、Mem0/LangMem/disabled）: [docs/memory-service-architecture.md](docs/memory-service-architecture.md)
- Mem0 graph backend 的私有 HTTP adapter 子集: [docs/memory_server_api_spec.md](docs/memory_server_api_spec.md)
- Context engineering architecture: [docs/context_engineering_status.md](docs/context_engineering_status.md)
- Multi-agent routing: [docs/agent-communication-routing.md](docs/agent-communication-routing.md)
- Media-Agent WebSocket contract: [docs/media-agent-service-websocket.md](docs/media-agent-service-websocket.md)
- 实时逐帧 VLM、语义关键帧、视觉提醒与历史找物: [docs/visual-perception-architecture.md](docs/visual-perception-architecture.md)
- Core pytest、临时 TDD 与 incubating 边界: [tests/README.md](tests/README.md)
- System eval、原生 evaluation target 与当前评测缺口: [evals/README.md](evals/README.md)

## Local Environment

The Python package is `assistant_agent` under `src/assistant_agent/`. The local conda environment remains `hello_agent`.

Provider profiles and external-provider configuration are documented in [docs/tool-calling-architecture.md](docs/tool-calling-architecture.md).

## Package Layout

`src/assistant_agent/` 按能力所有权组织，不保留通用 `services`、全局 `schemas` 或独立 `realtime`
收纳层：

| package | responsibility |
| --- | --- |
| `native_agent/` | 生产父 StateGraph、fast create_agent、planning 子图、Provider/Tool/Memory 装配 |
| `runtime/` | Tool、Provider、媒体、Context 与 durable task 仍复用的中立 DTO/外围治理模块；不拥有 Graph 生命周期 |
| `context/` | 尚未迁移入口使用的旧 Context compiler 与专项能力 |
| `skills/` | Skill 加载、召回、校验、目录、执行与持久化 |
| `tools/` | 具体 Tool/Plugin 实现；生产内建 Tool 自身实现标准 `BaseTool`，由 `native_agent.tools` 静态装配 |
| `agent_server/` | Agent Server graph factory、认证、公开 SDK client 与媒体 custom route |
| `gateway/` | 外围兼容入口仍复用的旧 wire/事件/取消小类型；不拥有生产 Agent Server 或 Graph 生命周期 |
| `media/` | 音频边缘适配、视频摄取/观察、统一 image/text embedding 及视觉 adapter |
| `automation/` | durable task、proactive wake 和通知 |
| `multi_agent/` | Agent routing、delegation、transport 和 A2A |
| `observability/` | 本地兼容 ledger、历史诊断与评测辅助；生产执行树使用 LangSmith native tracing |
| `improvement/` | 离线改进证据、提案、评估和报告 |
| `providers/` | 跨入口共享的 Provider 配置、错误治理和 adapter |
| `memory/` | Mem0 transport 与旧 Memory bundle 兼容；生产最小 backend 位于 `native_agent.memory` |
| `api/` | 不执行 Graph 的 callback 与共享协议模型；生产 HTTP 由 Agent Server 提供 |
| `mcp/` | MCP 配置、client、registration 和 server adapter |
| `config/` | 进程配置装配 |

领域模型和稳定协议由所属 package 就近维护，例如 `tools/models.py`、
`context/models.py` 和 `multi_agent/a2a_protocol.py`。

旧外围入口仍可通过 `MULTIMODAL_AGENT_TOOL_PLUGIN_MODULES` 使用动态 Python Plugin；该机制会执行所配置
module 的进程内代码，不是不可信代码沙箱。生产 `assistant-native-v2` 只使用受信静态 Tool 清单和官方 MCP
allowlist，不加载动态 module。具体边界见 Tool calling 文档。

长期记忆由父图固定的 `memory_recall` / `memory_commit` 节点定位，正文冻结在 `state.memory_context`。
composition root 一次只装配一个最小 `MemoryBackend`：默认离线 `disabled`，也可显式选择 Mem0、使用
LangGraph `BaseStore` 的 LangMem，或注入第三方 adapter。具体配置和 mock/real 边界见长期记忆架构文档。

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
