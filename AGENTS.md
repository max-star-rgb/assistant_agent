# AGENTS.md

本文件是 Codex / coding agent 的仓库级入口。开始仓库内任何非纯问答、非单条无副作用命令任务前，以本文件为准。README 只做人类快速导航；专项架构细节在 `docs/*.md` 权威文档中；源码和测试优先于过期 prose。

## 1. 项目与入口

项目名、发行名和 Python 包名均为 `assistant_agent`，源码在 `src/assistant_agent/`。默认 Python 使用本机 conda 环境 `hello_agent`，除非用户明确要求，不要重命名环境路径。

本项目是本地优先的助理 Agent。默认运行、测试和 eval 只走 mock/local/offline；真实 Provider 必须通过 `provider_smoke` / `pilot` runtime profile 和本机未跟踪配置显式启用。

开始任务时，先按任务类型读取对应 `docs/*.md` 权威文档；如果文档与当前源码不一致，以源码和测试为准，并在本次变更中回补文档。项目 skill 只作为 workflow 检查清单或脚本入口，不作为事实权威。

| task | read first |
| --- | --- |
| Gateway、realtime、WebSocket、Media-Agent | `docs/gateway-architecture.md`；`docs/media-agent-service-websocket.md` |
| assistant loop、runtime stream、provider stream | `docs/runtime-event-stream-architecture.md` |
| tool calling、MCP、durable task、provider 调用治理 | `docs/tool-calling-architecture.md` |
| memory、本地/外部记忆服务、记忆读写策略 | `docs/memory-service-architecture.md`；`docs/memory_server_api_spec.md` |
| context、prompt、conversation history、context budget | `docs/CONTEXT_ENGINEERING_STATUS.md` |
| multi-agent、A2A、delegation | `docs/agent-communication-routing.md` |
| trace、observability、redaction | `docs/observability-harness.md` |
| 测试分层和 scope 选择 | `tests/README.md`；`tests/scope-map.toml` |

`docs/development/**`、`docs/superpowers/**` 和 `docs/interview/**` 都不是普通开发默认权威；只有用户点名、运行 runbook 或做历史/面试任务时才读。

## 2. 架构边界

硬边界：

- 入口层只负责接入和归一化请求；主运行时仍是 `AgentGraphRuntime` / assistant loop。
- Gateway 负责 session/run/cancel/interrupt/reconnect/stream frame 生命周期，不承担主大脑职责。
- 所有工具调用和外部副作用必须经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- Provider 默认只能走 mock/local/offline；真实 Provider 必须由 `provider_smoke` / `pilot` profile 和显式配置启用，不能因为检测到 key 自动启用。
- Memory 读写必须经过 `MemoryManager`、read/write policy、store/audit 边界；memory tool 保持薄适配。
- MCP、durable task、A2A、API、CLI、demo、eval 都是入口或调度形态，不能绕过 runtime、tool、provider、memory 治理链路。
- API、demo、eval、CLI 应复用同一套 runtime 行为，避免各自实现 Agent 逻辑。
- 非 Python 的 Web UI、BFF、vendor adapter 或边缘入口只能做薄适配器；不要把旧 `runTime` agent loop 引入本项目。

## 3. 运行与安全

默认保持离线安全：

- 测试、eval、无 key 环境只走 mock/local/offline。
- 真实 Provider 只能在用户明确要求、`provider_smoke` / `pilot` profile、具体 provider 显式配置同时满足时调用；不能因为检测到 key 自动启用。
- 不写入或提交 API key、token、真实 `.env`、真实用户数据、provider 原始响应、真实媒体、大文件、缓存或生成物。
- 不主动安装新依赖、不联网拉取依赖，除非用户明确要求并允许；需要安装时先询问用户。
- 如果本轮调用了真实 Provider，最终报告必须说明调用范围和验证结果。

## 4. 本地命令

默认使用：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

常用验证：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --changed BASE..HEAD -- -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --full -- -q
```

测试 scope、marker、新增测试方法和 `--full` 触发条件以 `tests/README.md` 和 `tests/scope-map.toml` 为准。服务、demo、eval、smoke 和 runbook 命令按 README、`scripts/README.md` 或对应 `docs/*.md` 执行。只有在需要 conda 激活环境变量时才使用 `conda run -n hello_agent <command>`。

## 5. 目录与编辑策略

| path | responsibility |
| --- | --- |
| `src/assistant_agent/api/` | FastAPI routes、WebSocket、auth、task/trial/control-plane 入口 |
| `src/assistant_agent/gateway/`, `realtime/` | Gateway lifecycle、realtime adapter、Gateway 到 assistant runtime 的薄 adapter |
| `src/assistant_agent/agent/`, `services/` | LangGraph runtime、assistant loop、context、trace、session、agent communication、provider 管理 |
| `src/assistant_agent/services/context/` | context source、conversation、tool catalog、prompt compiler/renderer、budget/compaction |
| `src/assistant_agent/services/durable_tasks/`, `api/routes_tasks.py` | durable task store/service/worker、confirmation/input/cancel API |
| `src/assistant_agent/services/improvement/`, `services/proactive_wake/` | 离线人工评审改进候选；显式规则驱动的本地 proactive wake、SQLite outbox 与投递 |
| `src/assistant_agent/services/realtime_task_state.py`, `realtime_video_*`, `video_context.py`, `agent_service_latency.py` | realtime task/call 状态、视频观察上下文与 turn latency 诊断；只保存 prompt-safe 状态/引用/统计 |
| `src/assistant_agent/tools/`, `mcp/` | Tool registry/工具实现、MCP wrapper 和显式工具入口 |
| `src/assistant_agent/memory/`, `services/memory_*.py` | local/remote/framework memory core、policy、audit、snapshot、media ingestion |
| `src/assistant_agent/providers/`, `video_ai/`, `services/provider_*.py`, `config.py`, `runtime_profile.py` | provider adapter、profile、provider selection/readiness/diagnostics/budget、实时视频本地处理 |
| `src/assistant_agent/schemas/` | Pydantic/API/tool/provider/memory/gateway/runtime contract |
| `tests/`, `scripts/` | pytest 测试、本地验证、服务、demo、eval、smoke 脚本 |
| `docs/*.md` | 当前架构、接口和状态权威文档 |
| `docs/development/`, `docs/superpowers/`, `docs/interview/` | 非默认权威材料：runbook、历史计划/spec、面试资料 |
| `.codex/skills/` | 少量项目 workflow、检查清单和脚本；不作为事实权威，不复制长篇架构细节 |

修改行为时同步维护相关测试和文档。若用户设定更严格 scope，例如“不要修改 `src/**`”或“不要修改 `tests/**`”，以用户当前约束为准。

## 6. 编码约定

- 新代码优先放入 `src/assistant_agent/` 既有分层；公共数据结构优先使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串；工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 外部模型/API 先维护 adapter interface、resolved provider spec 和 mock/unconfigured implementation，不直接把入口层绑定到具体供应商。
- 多 agent 通信先维护内部 message/task/artifact contract 和 transport adapter；A2A JSON-RPC 只作为协议适配层。
- Memory tool 保持薄层，不在 `tools/memory_tool.py` 新增检索排序、写入策略、画像合并、TTL、审计或直接 store 访问。
- Memory 工具选择采用 LLM-first：assistant loop 由 LLM 语义判断是否调用 `memory_save` / `memory_retrieval`，`memory_save` 声明 `source_intent`，当前不以 keyword/vector 规则覆盖 `source_intent` 来决定选择，任何写入仍必须经过 `MemoryWritePolicy`。
- 长期记忆读取和自动注入必须经过 `MemoryReadPolicy`；普通首次文案、建议、搜索、生成或推荐不自动查长期记忆，`memory_retrieval` / legacy retrieve 也必须先过 `ActionValidator` 读取意图 gate。
- 真实 LLM 路径以 provider-native content/tool calls 主循环为准；不要依赖旧 intent/router/plan 强制选择工具或最终答案。
- Durable task worker/resume 只能执行当前 task binding 允许的 ready step；不能把任务恢复路径做成绕过工具治理的后台脚本。
- 工具系统设计收敛以 `docs/tool-calling-architecture.md` 的“设计收敛原则”为准；不要整体照搬 Hermes、LangChain、OpenClaw 或 Claude Code 的工具系统。
- mock/offline 路径只作为稳定测试与本地演示兼容层，不把 mock 行为伪装成真实 LLM 能力。
- 新增核心 ReAct/assistant loop 测试优先覆盖非 mock LLM 决策路径，例如 scripted/fake real chat adapter；真实外部网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 7. 文档与工作模式

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定；`README.md` 是人类轻导航入口。
- 当前架构权威文档只保留在 `docs/*.md`；新增、删除或重命名 root authority 时，同步更新第 1 节路由表和 README。
- 走读文档只解释已沉淀机制，不替代权威文档；`docs/development/**`、`docs/superpowers/**` 和 `docs/interview/**` 不作为普通开发默认权威；新增文档必须有明确长期用途。
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
