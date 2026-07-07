# AGENTS.md

本文件是 Codex / coding agent 的仓库级入口。开始仓库内任何非纯问答、非单条无副作用命令任务前，以本文件为准。README 面向人类快速导航；专项架构细节保留在少量 `docs/` 权威文档中。

## 1. 项目定位

本仓库项目名、展示名、发行名和 Python 包名均为 `assistant_agent`，包目录为 `src/assistant_agent/`。本地 conda 环境仍为 `hello_agent`，除非用户明确要求，不要重命名环境路径。

项目实现一个本地优先的多模态自主工具调用 Agent。Agent 负责理解用户输入、选择工具、执行受控调用、融合结果并给出最终回答；具体能力由工具、provider adapter、memory service、demo/eval/API 层协作提供。

当前核心运行时以 LangGraph/ReAct assistant loop 为主，同时保留 mock/local/offline 路径用于稳定测试和演示。真实外部 Provider 必须通过 `provider_smoke` 或 `pilot` profile 和本机未跟踪配置显式启用。

## 2. 当前权威入口

按任务范围优先使用项目内 `.codex/skills/**`。skill 是执行工作流包装，负责按需路由到对应权威 docs、源码和测试；AGENTS 不重复列出每个 skill 内部的补充阅读清单。

| scope | entry |
| --- | --- |
| Gateway、realtime frame、session/run/cancel/interrupt、WebSocket bridge、旧 `runTime` 参考边界 | `.codex/skills/assistant-runtime-reference`，权威文档是 `docs/gateway-architecture.md` |
| tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、MCP `tool_run`、工具 observation/retry/budget | `.codex/skills/assistant-agent-tool-calling`，权威文档是 `docs/tool-calling-architecture.md` |
| 记忆服务、MemoryManager、Memory Kernel、memory store/retrieval/write policy、memory API、user profile、audit、retention | `.codex/skills/assistant-agent-memory-service`，权威文档是 `docs/memory-service-architecture.md` |
| context engineering、prompt/context rendering、conversation history、memory context、tool observation compaction、context budget | `.codex/skills/assistant-agent-context-engineering`，权威文档是 `docs/CONTEXT_ENGINEERING_STATUS.md` |
| 多 agent、`assistant_agent.agent_routing`、AgentRouter、AgentDirectory、A2A/JSON-RPC、`delegate_to_agent`、pilot readiness | `.codex/skills/assistant-agent-collaboration`，权威文档是 `docs/agent-communication-routing.md` |
| 状态日志、trace、运行监控、ReAct 关键节点观测、redaction | `docs/observability-harness.md` |
| 面试训练、题库、回答点评、标准答案、面试文档更新 | `.codex/skills/assistant-agent-interview-trainer`，权威文档是 `docs/interview/README.md` |

`docs/development/**` 只保留仍有现实用途的操作 runbook 或用户明确点名的执行材料，不作为默认设计权威。不要把旧 roadmap 或阶段计划当作当前架构。

## 3. 当前架构边界

核心调用链：

```text
User / CLI / API / Web UI
        |
        v
FastAPI routes or local runner
        |
        v
AgentGraphRuntime / assistant loop
        |
        v
AssistantDecision -> ActionValidator -> ToolExecutor
        |
        v
ToolRegistry -> tools -> provider adapters / memory / local services
```

重要边界：

- Agent/graph 负责决策编排，不直接绕过工具治理边界调用外部能力。
- 工具调用必须经过 validator、executor、tool registry、policy/audit 相关边界。
- Provider adapter 负责真实或 mock 能力接入；默认 profile 必须是 mock/local/offline。
- Memory 行为应通过 memory service/provider 管理，不把临时状态散落到无关模块。
- Memory tools 只是 Agent 可调用适配器，不是记忆服务所有者；检索、写入策略、TTL、去重、用户画像、审计和 store 选择必须留在 `MemoryManager`、`memory/` 或 `services/memory_*`。
- 多 agent / A2A 行为应通过 `assistant_agent.agent_routing` 聚合入口、AgentRouter、agent communication service、directory、transport adapter 和工具治理边界管理。
- CLI、Web UI、App、HTTP、WebSocket 和 realtime call adapter 属于入口层；Gateway 是入口层之后的标准化消息、session/run 生命周期、cancel/interrupt、reconnect/hangup 和 stream frame 控制边界。
- `assistant_agent.realtime` / `GatewayAgentAdapter` 是 Gateway 到当前主运行时的薄适配层，不承担主大脑职责。
- 当前核心 runtime、Gateway session/run/cancel/interrupt/history 等生命周期权威实现继续以 Python `assistant_agent` 为主。新增 Web UI、BFF、vendor WebSocket adapter、Media Relay adapter、边缘入口或电话/实时媒体 SDK 适配层可以使用 TypeScript、Go、Rust 等非 Python 语言，但这些层必须保持薄入口适配器职责。
- AgentRouter 只负责内部 agent 选择、capability routing、controller/worker route 和控制面记录；面向调用方的多 agent 导入优先走 `assistant_agent.agent_routing`。
- 不要把 `runTime` 的旧 OpenClaw/Anthropic agent loop 引入本项目；当前 agent 内部执行器仍是 `AgentGraphRuntime` / assistant loop。
- API、demo、eval、CLI 应尽量复用同一套 runtime 行为，避免各自实现不一致的 Agent 逻辑。

## 4. 运行与安全规则

默认规则不可随意放宽：

- 仓库测试、eval、无 key 环境默认只允许 mock/local/offline 路径。
- 用户本机任务可以使用真实 LLM，但不自动调用真实图片、视频、商品、通知、数据库或其他外部 Provider。
- 不因为检测到 API key 就启用真实 Provider。
- 不写入 API key、token、真实 `.env`、真实用户数据或真实 provider raw response。
- 不提交真实媒体、生成物、大文件、缓存目录或外部服务原始返回。
- 不安装新依赖，不联网拉取依赖，除非用户明确要求并允许。

真实 Provider 调用必须同时满足：

- 用户或任务明确要求真实 Provider，或本轮任务是在用户本机真实 LLM 运行口径下执行。
- 使用受控 runtime profile，例如 `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke` 或 `pilot`。
- key 只来自本机环境变量或用户已配置的安全位置，不能写入仓库。
- 最终报告必须说明调用范围和验证结果。

## 5. 本地 Python 环境

默认使用 conda 环境 `hello_agent`。Codex 执行 Python、pytest、脚本时，优先直接调用：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

常用命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --provider mock --image-provider mock
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_client.py --server http://127.0.0.1:8000 "你好"
```

只有在需要执行非 Python 命令且依赖 conda 激活环境变量时，才使用：

```bash
conda run -n hello_agent <command>
```

## 6. 目录与编辑策略

| path | responsibility | default edit policy |
| --- | --- | --- |
| `src/assistant_agent/api/` | FastAPI app、routes、server/client integration | 按任务要求修改 |
| `src/assistant_agent/gateway/` | Gateway protocol、bridge、session/run/cancel/interrupt、realtime frame lifecycle | 按任务要求修改，边界以 `docs/gateway-architecture.md` 为准 |
| `src/assistant_agent/realtime/` | Gateway 到 assistant runtime 的薄 adapter/backend contract 和 event mapping | 按任务要求修改 |
| `src/assistant_agent/agent/` | LangGraph runtime、assistant loop、决策、验证、执行 | 按任务要求修改 |
| `src/assistant_agent/services/` | runtime services、context、trace、session、agent communication、provider 管理 | 按任务要求修改 |
| `src/assistant_agent/providers/` | Provider adapter、runtime profile、mock/real 边界 | 谨慎修改，默认 mock 优先 |
| `src/assistant_agent/tools/` | Tool registry、工具实现、策略、审计 | 按任务要求修改 |
| `src/assistant_agent/memory/` | 记忆服务、检索、存储 | 按任务要求修改 |
| `src/assistant_agent/eval/` | 离线评测逻辑 | 按任务要求修改 |
| `tests/` | pytest 测试 | 修改行为时同步维护，除非用户限制只读 |
| `scripts/` | 本地验证、服务、demo、eval、smoke 脚本 | 可按任务修改 |
| `docs/` | 当前权威文档、走读文档、API/runbook、面试资料 | 文档任务优先修改 |
| `.codex/skills/` | 项目内 Codex skills，包装专项工作流并路由到权威 docs | 只放项目专用 workflow，不复制长篇架构细节 |

如果用户对本轮任务设定更严格的 scope，例如“不要修改 `src/**`”或“不要修改 `tests/**`”，以用户当前约束为准。

## 7. 编码约定

- 新代码优先放入 `src/assistant_agent/` 的既有分层。
- 公共数据结构优先使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串。
- 外部模型/API 先维护 adapter interface 和 mock implementation，不要直接绑定具体供应商。
- 多 agent 通信先维护内部 message/task/artifact contract 和 transport adapter；A2A JSON-RPC 只作为协议适配层，不作为核心 runtime 内部模型。
- Memory tool 代码应保持薄层，只做 `ToolContext` 身份绑定、输入适配、调用 `MemoryManager`、包装 `ToolResult`；不要在 `tools/memory_tool.py` 新增检索排序、写入策略、画像合并、TTL、审计或直接 store 访问。
- Memory 工具选择采用 LLM-first：assistant loop 由 LLM 语义判断是否调用 `memory_save` / `memory_retrieval`，`memory_save` 声明 `source_intent`，当前不以 keyword/vector 规则覆盖 `source_intent` 来决定选择，任何写入仍必须经过 `MemoryWritePolicy`。
- Phase 8 之后的 assistant loop 方向是真实 LLM 自主决策、追问、工具调用和最终回答；不要让真实 LLM 路径依赖旧 intent/router/plan 来选择工具。
- mock/offline 路径只作为稳定测试与本地演示兼容层，不要把 mock 行为伪装成真实 LLM 能力。
- 新增核心 ReAct/assistant loop 测试应优先覆盖非 mock LLM 决策路径，例如 scripted/fake real chat adapter；真实外部网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 修改行为时同步更新相关测试和文档。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 8. 文档维护规则

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定，不塞入长篇历史设计。
- `README.md` 是人类轻导航入口，不承担专项架构权威职责。
- 当前架构权威文档是 `docs/gateway-architecture.md`、`docs/tool-calling-architecture.md`、`docs/observability-harness.md`、`docs/memory-service-architecture.md`、`docs/CONTEXT_ENGINEERING_STATUS.md` 和 `docs/agent-communication-routing.md`。
- 走读文档只用于解释已沉淀机制，不替代权威文档。
- `docs/development/**` 只保留仍有现实用途的操作 runbook 或用户明确点名的执行材料。
- `docs/interview/**` 是面试训练资料，普通开发任务不把它当作架构来源。
- `.codex/skills/**` 只包装项目专用工作流和入口路由，不取代对应权威 docs。
- 新增文档必须有明确长期用途；优先更新 AGENTS、README 或现有专项文档。

## 9. 测试与验收

小开发优先使用快测层和相关专项测试：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/unit tests/contracts -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/<relevant_test_file>.py -q
```

完整离线验证：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

如环境安装了工具，可补充：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff format --check .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m mypy src
```

如果命令不存在或失败，不要为了让测试过而擅自改无关源码；应记录命令、结果和失败原因。

## 10. 工作模式

开始任务时：

```text
我将处理：...
我会先阅读：...
计划：...
```

执行过程中：

- 先读相关代码和文档，再做判断。
- 保持 scope 小而明确，不跨任务提前实现未来能力。
- 手工新建或修改文件默认使用 `apply_patch`。
- 使用 `rg` / `rg --files` 搜索文件和文本。
- 不回滚用户已有改动；遇到 dirty worktree 时先识别改动来源，和当前任务无关则保持不动。
- 不用 `python -c` 绕过任务 scope 写文件；它只用于受控机械替换、环境检查或小范围验证。

结束任务时：

```text
完成内容：...
修改文件：...
测试结果：...
未完成/限制：...
下一步建议：...
```

## 11. 业务专项说明

涉及好单库相关功能时，先读取：

- `haodanku-openapi-docs/AI使用说明.md`
- `haodanku-openapi-docs/接口目录.md`

再按意图路由到对应分类文档编码。不要跳过本地 validator、executor、policy、audit 边界直接调用外部服务。
