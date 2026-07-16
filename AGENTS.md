# AGENTS.md

本文件是 Codex / coding agent 的仓库级入口。开始仓库内任何非纯问答、非单条无副作用命令任务前，以本文件为准。README 只做人类快速导航；专项架构细节保留在少量 `docs/*.md` 权威文档中。

## 1. 项目与入口

项目名、展示名、发行名和 Python 包名均为 `assistant_agent`，包目录为 `src/assistant_agent/`。本地 conda 环境仍为 `hello_agent`，除非用户明确要求，不要重命名环境路径。

项目实现一个本地优先的助理 Agent。当前核心运行时以 LangGraph/ReAct assistant loop 为主，同时保留 mock/local/offline 路径用于稳定测试和演示。真实外部 Provider 必须通过 `provider_smoke` 或 `pilot` profile 和本机未跟踪配置显式启用。

按任务范围优先使用项目内 `.codex/skills/**`；skill 负责路由到对应权威 docs、源码和测试，AGENTS 不复制 skill 内部补充阅读清单。

`docs/*.md` 是当前权威文档层，只保留当前架构、接口或状态权威。路线图、runbook、历史计划、走读说明、题库和 workflow 材料必须放在 `docs/<subdir>/`，不得作为默认架构权威。

| scope | entry |
| --- | --- |
| Gateway、realtime frame、session/run/cancel/interrupt、WebSocket bridge、`/agent-service/v1` Media-Agent 协议、旧 `runTime` 参考边界 | `.codex/skills/assistant-runtime-reference`；`docs/gateway-architecture.md`；`docs/media-agent-service-websocket.md` |
| runtime/provider streaming、`LLMEvent`、`AgentEvent`、`AgentRunStream`、stream/result、线程桥接 | `.codex/skills/assistant-agent-runtime-streaming`；`docs/runtime-event-stream-architecture.md` |
| tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、MCP `tool_run`、工具 observation/retry/budget | `.codex/skills/assistant-agent-tool-calling`；`docs/tool-calling-architecture.md` |
| 记忆服务、MemoryManager、local memory store/retrieval/write policy、外部 Memory Service 接口、user profile、audit、retention | `.codex/skills/assistant-agent-memory-service`；`docs/memory-service-architecture.md`；`docs/memory_server_api_spec.md` |
| context engineering、prompt/context rendering、conversation history、memory context、tool observation compaction、context budget | `.codex/skills/assistant-agent-context-engineering`；`docs/CONTEXT_ENGINEERING_STATUS.md` |
| 多 agent、`assistant_agent.agent_routing`、AgentRouter、AgentDirectory、A2A/JSON-RPC、`delegate_to_agent`、pilot readiness | `.codex/skills/assistant-agent-collaboration`；`docs/agent-communication-routing.md` |
| trace、运行监控、ReAct 关键节点观测、redaction | `docs/observability-harness.md` |
| 面试训练、题库、回答点评、标准答案、面试文档更新 | `.codex/skills/assistant-agent-interview-trainer`；`docs/interview/README.md` |
| 功能实现、缺陷修复或行为重构中的测试决策、阶段验收和测试保留 | `.codex/skills/assistant-agent-development-testing`；普通行为开发自动触发 |
| 用户显式请求的全仓文档同步、漂移审计、权威对齐或失效文档清理 | `.codex/skills/assistant-agent-documentation-sync`；不得因普通代码变更隐式触发 |
| 用户显式请求的测试审计、去重、分层、marker 治理或测试清理 | `.codex/skills/assistant-agent-test-governance`；`tests/README.md`；不得因普通功能开发隐式触发 |

`docs/development/**` 只保留仍有现实用途的操作 runbook 或用户明确点名的执行材料，不作为默认设计权威。`docs/roadmaps/**` 只保留长期方向和 north star，不替代当前权威文档。`docs/superpowers/**` 是设计/实施记录，`docs/interview/**` 是面试训练材料；它们都不作为普通开发默认权威。不要把旧 roadmap、阶段计划或走读说明当作当前架构。

## 2. 架构边界

核心调用链：

```text
User / CLI / API / Web UI
  -> FastAPI routes or local runner
  -> AgentGraphRuntime / assistant loop
  -> AssistantDecision -> ActionValidator -> ToolExecutor
  -> ToolRegistry -> tools -> provider adapters / memory / local services
```

硬边界：

- Agent/graph 负责编排，不绕过工具治理边界直接调用外部能力。
- 工具调用必须经过 validator、executor、tool registry、policy/audit 相关边界。
- Provider adapter 负责真实或 mock 能力接入；默认 profile 必须是 mock/local/offline。
- Memory 行为归 `MemoryManager`、`memory/` 或 `services/memory_*`；memory tools 只做 `ToolContext` 身份绑定、输入适配、调用服务和包装 `ToolResult`。
- Memory 采用内置 local core 与可选外部 Memory Service core 的双核边界；两者都必须经过同一套 identity、read/write policy、manager/store、audit/snapshot/export 治理链路。
- `memory_backend=framework` 是显式启用的本地 sidecar lifecycle-owner 模式；Hindsight/Mem0 只能通过 `MemoryManager -> FrameworkMemoryStore -> MemoryEngineAdapter` 接入，主环境不安装框架依赖，框架不得注册 Agent runtime 或绕过工具治理。
- 多 agent / A2A 行为走 `assistant_agent.agent_routing`、AgentRouter、agent communication service、directory、transport adapter 和工具治理边界。
- CLI、Web UI、App、HTTP、WebSocket、realtime call adapter 属于入口层；Gateway 是入口层之后的标准化消息、session/run 生命周期、cancel/interrupt、reconnect/hangup 和 stream frame 控制边界。
- `assistant_agent.realtime` / `GatewayAgentAdapter` 是 Gateway 到当前主运行时的薄适配层，不承担主大脑职责。
- 当前核心 runtime 和 Gateway 生命周期权威实现继续以 Python `assistant_agent` 为主。新增 Web UI、BFF、vendor WebSocket adapter、Media Relay adapter、边缘入口或电话/实时媒体 SDK 适配层可以使用 TypeScript、Go、Rust 等非 Python 语言，但只能做薄入口适配器。
- 不要把 `runTime` 的旧 OpenClaw/Anthropic agent loop 引入本项目；当前 agent 内部执行器仍是 `AgentGraphRuntime` / assistant loop。
- API、demo、eval、CLI 应复用同一套 runtime 行为，避免各自实现不一致的 Agent 逻辑。

## 3. 运行与安全

默认规则不可随意放宽：

- 仓库测试、eval、无 key 环境默认只允许 mock/local/offline 路径。
- 用户本机任务可以使用真实 LLM，但不自动调用真实图片、视频、商品、通知、数据库或其他外部 Provider。
- 不因为检测到 API key 就启用真实 Provider。
- 不写入 API key、token、真实 `.env`、真实用户数据或真实 provider raw response；key 只来自本机环境变量或用户已配置的安全位置，不能写入仓库。
- 不提交真实媒体、生成物、大文件、缓存目录或外部服务原始返回。
- 不安装新依赖，不联网拉取依赖，除非用户明确要求并允许。

真实 Provider 调用必须同时满足：用户或任务明确要求真实 Provider，使用 `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke` 或 `pilot`，并在最终报告说明调用范围和验证结果。

## 4. 本地命令

默认使用 conda 环境 `hello_agent`。Codex 执行 Python、pytest、脚本时，优先直接调用：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

常用验证：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --scope tools -- -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --changed BASE..HEAD -- -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --full -- -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

测试反馈按层级执行，避免普通开发反复承担全量套件成本：

日常行为开发先使用 `.codex/skills/assistant-agent-development-testing` 决定新增、扩展、复用、阶段暂存或不新增测试；不得把“使用 TDD”理解为无条件新建测试文件。

1. 开发循环运行新增测试和直接相关回归；阶段结束使用 `run_scoped_tests.py --scope ...`，已提交范围可使用 `--changed BASE..HEAD`。
2. 涉及跨层功能且窄层无法证明 wiring 时，在提交前补充并运行一条离线端到端测试，贯穿真实仓库调用链，但使用 scripted/fake Provider，禁止默认联网。
3. `--full` 触发门槛以 `tests/README.md` 为准，不要求每轮局部修改后运行。
4. scoped 测试结构、scope 列表、marker 和新增测试方法以 `tests/README.md` 为准。

本地 mock 服务：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --provider mock --image-provider mock
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/realtime_media_client.py --server http://127.0.0.1:8000 --scenario basic
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_gateway_client.py --server http://127.0.0.1:8000 "你好"
```

只有在需要执行非 Python 命令且依赖 conda 激活环境变量时，才使用 `conda run -n hello_agent <command>`。

## 5. 目录与编辑策略

| path | responsibility |
| --- | --- |
| `src/assistant_agent/api/`, `gateway/`, `realtime/` | FastAPI/API、Gateway lifecycle、Gateway 到 assistant runtime 的薄 adapter |
| `src/assistant_agent/agent/`, `services/` | LangGraph runtime、assistant loop、context、trace、session、agent communication、provider 管理 |
| `src/assistant_agent/services/improvement/`, `proactive_wake/` | 离线人工评审改进候选；显式规则驱动的本地 proactive wake、SQLite outbox 与投递 |
| `src/assistant_agent/services/realtime_task_state.py`, `realtime_video_*`, `video_context.py`, `agent_service_latency.py` | realtime task/call 状态、视频观察上下文与 turn latency 诊断；只保存 prompt-safe 状态/引用/统计 |
| `src/assistant_agent/tools/`, `memory/`, `providers/`, `eval/` | Tool registry/工具实现、记忆服务、provider adapter、离线评测 |
| `tests/`, `scripts/` | pytest 测试、本地验证、服务、demo、eval、smoke 脚本 |
| `docs/*.md` | 当前架构、接口和状态权威文档 |
| `docs/development/`, `docs/roadmaps/`, `docs/superpowers/`, `docs/interview/` | 非默认权威材料：runbook、长期路线图、历史计划/spec、面试资料 |
| `.codex/skills/` | 项目专用 workflow 和入口路由，不复制长篇架构细节 |

修改行为时同步维护相关测试和文档。若用户设定更严格 scope，例如“不要修改 `src/**`”或“不要修改 `tests/**`”，以用户当前约束为准。

## 6. 编码约定

- 新代码优先放入 `src/assistant_agent/` 既有分层；公共数据结构优先使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串；工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 外部模型/API 先维护 adapter interface 和 mock implementation，不直接绑定具体供应商。
- 多 agent 通信先维护内部 message/task/artifact contract 和 transport adapter；A2A JSON-RPC 只作为协议适配层。
- Memory tool 保持薄层，不在 `tools/memory_tool.py` 新增检索排序、写入策略、画像合并、TTL、审计或直接 store 访问。
- Memory 工具选择采用 LLM-first：assistant loop 由 LLM 语义判断是否调用 `memory_save` / `memory_retrieval`，`memory_save` 声明 `source_intent`，当前不以 keyword/vector 规则覆盖 `source_intent` 来决定选择，任何写入仍必须经过 `MemoryWritePolicy`。
- 长期记忆读取和自动注入必须经过 `MemoryReadPolicy`；普通首次文案、建议、搜索、生成或推荐不自动查长期记忆，`memory_retrieval` / legacy retrieve 也必须先过 `ActionValidator` 读取意图 gate。
- Phase 8 之后的 assistant loop 方向是真实 LLM 自主决策、追问、工具调用和最终回答；真实 LLM 路径不要依赖旧 intent/router/plan 来选择工具。
- 工具系统设计收敛以 `docs/tool-calling-architecture.md` 的“设计收敛原则”为准；不要整体照搬 Hermes、LangChain、OpenClaw 或 Claude Code 的工具系统。
- mock/offline 路径只作为稳定测试与本地演示兼容层，不把 mock 行为伪装成真实 LLM 能力。
- 新增核心 ReAct/assistant loop 测试优先覆盖非 mock LLM 决策路径，例如 scripted/fake real chat adapter；真实外部网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 7. 文档与工作模式

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定；`README.md` 是人类轻导航入口。
- 当前架构权威文档只保留在 `docs/*.md`，包括 `docs/gateway-architecture.md`、`docs/media-agent-service-websocket.md`、`docs/runtime-event-stream-architecture.md`、`docs/tool-calling-architecture.md`、`docs/observability-harness.md`、`docs/memory-service-architecture.md`、`docs/memory_server_api_spec.md`、`docs/CONTEXT_ENGINEERING_STATUS.md` 和 `docs/agent-communication-routing.md`。
- 走读文档只解释已沉淀机制，不替代权威文档；`docs/development/**`、`docs/roadmaps/**`、`docs/superpowers/**` 和 `docs/interview/**` 不作为普通开发默认权威；新增文档必须有明确长期用途。
- 开始任务时说明“我将处理 / 我会先阅读 / 计划”；执行中先读相关代码和文档，保持 scope 小而明确。
- 手工新建或修改文件默认使用 `apply_patch`；搜索优先用 `rg` / `rg --files`。
- 不回滚用户已有改动；遇到 dirty worktree 时先识别来源，无关改动保持不动。
- 当本地开发 UX 依赖固定脚本参数、日志页签或调试入口时，优先同步维护共享 PyCharm `.run/*.run.xml` 配置，把已验证的 script path、parameters、必要环境变量和日志文件页签直接写好；不得写入密钥、真实 `.env` 路径、用户私有数据或只适合个人机器的临时值。
- 创建设计文档时不单独提交；在对应开发阶段完成并通过验证后，再统一提交该阶段的代码、测试和文档。
- 对用户明确要求实施、修复或完成计划的开发任务，代码、测试和文档验证通过后，默认创建本地 Git commit，无需再次询问。只提交本任务相关文件，不包含用户已有或无关改动。除非用户明确要求，否则不 push、不合并、不创建 PR；纯问答、诊断和只读审查不提交。
- 不用 `python -c` 绕过任务 scope 写文件；它只用于受控机械替换、环境检查或小范围验证。
- 结束任务时报告完成内容、修改文件、测试结果、未完成/限制和下一步建议。

## 8. 业务专项

涉及好单库相关功能时，先读取：

- `haodanku-openapi-docs/AI使用说明.md`
- `haodanku-openapi-docs/接口目录.md`

再按意图路由到对应分类文档编码。不要跳过本地 validator、executor、policy、audit 边界直接调用外部服务。
