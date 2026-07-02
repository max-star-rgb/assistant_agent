# AGENTS.md

本文件是给 Codex / coding agent 的仓库级指导。它是当前唯一工作入口，应保持稳定、可自动加载，并只记录当前项目的通用规则。README 暂时只是占位入口；专项架构细节只保留在少量 `docs/` 文档中。

## 1. 当前权威入口

开始仓库内任何非纯问答/非单条无副作用命令任务前，以本文件为准。只有与仓库无关的普通问答，或用户只要求执行单个简单命令时可跳过补充阅读。

按任务范围优先使用项目内 `.codex/skills/**`。skill 是执行工作流包装，负责按需路由到对应权威 docs、源码和测试；AGENTS 不重复列出每个 skill 内部的补充阅读清单。

- 涉及上下文工程、assistant context、prompt/context rendering、conversation history、memory context、tool observation compaction 或 context budget 时，如可用优先使用项目内 `.codex/skills/assistant-agent-context-engineering` skill。
- 涉及 tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、provider-native tool calls、MCP `tool_run`、工具 observation、工具执行预算/retry/recovery 或新增/修改工具调用链时，如可用优先使用项目内 `.codex/skills/assistant-agent-tool-calling` skill。
- 涉及记忆服务设计、`MemoryManager`、Memory Kernel、memory store/retrieval/write policy/user profile、memory tool、memory API、SQLite/store migration、RequestIdentity、token-aware memory context、retention/export/audit、memory eval 或长期记忆边界时，如可用优先使用项目内 `.codex/skills/assistant-agent-memory-service` skill。
- 涉及多 agent 实例、`assistant_agent.agent_routing` 聚合入口、agent directory、AgentRouter、agent-to-agent 通信、`/agents/run`、A2A/JSON-RPC adapter、跨实例 session/task 路由、`delegate_to_agent`、pilot readiness、agent router evidence 或 OpenClaw 概念映射时，如可用优先使用项目内 `.codex/skills/assistant-agent-collaboration` skill。
- 涉及 `assistant_agent.gateway`、realtime phone/gateway、wire protocol frame、`message.user`/`run.started`/`stream.chunk`/`run.end`/`run.cancel` 语义、session history、cancel/interrupt/multiturn 行为、WebSocket gateway bridge，或需要对照 `/home/lenovo1/pycharm_project/runTime` 的参考实现时，如可用必须使用项目内 `.codex/skills/assistant-runtime-reference` skill；该 skill 只用于查看旧 runtime 参考实现，不把 `runTime` 重新作为当前项目入口。
- 涉及面试训练、模拟面试、题目分级、回答点评、标准答案、面试金句或 `docs/interview/` 文档更新时，如可用优先使用项目内 `.codex/skills/assistant-agent-interview-trainer` skill。
- 涉及架构分层、模块归属、治理边界或重构判断时，以本文件的“当前架构边界”和“编码约定”为准；只有触及上下文、tool calling、记忆、agent collaboration 或 OpenClaw 参考映射专项时才使用对应项目 skill/权威材料。
- 涉及文档盘点、入口路由、归档、删除、清理或新增文档时，优先保持 AGENTS 和少量专项 docs 同步；README 只保留占位，不要重新引入通用索引文档。

`docs/development/**` 是历史开发计划、阶段记录和 runbook，默认不作为新任务的必读入口或当前设计权威；只有用户明确点名、需要追溯历史决策，或执行文档清理/归档任务时才阅读。历史 task、prompt 和旧 runner skill 构建材料已按用户确认删除；少量剩余 phase/archive 背景文档也只在用户明确点名、需要追溯历史决策或执行对应历史任务时阅读。不要把旧 roadmap 当成当前真实架构。

## 2. 项目定位

本仓库项目名为 `assistant_agent`，实现一个本地优先的多模态自主工具调用 Agent。Agent 负责理解用户输入、选择工具、执行受控调用、融合结果并给出最终回答；具体能力由工具、provider adapter、memory service、demo/eval/API 层协作提供。

当前项目展示名/发行名和 Python 包名均为 `assistant_agent`，包目录为 `src/assistant_agent/`；本地 conda 环境仍为 `hello_agent`，除非用户明确要求，不要擅自重命名环境路径。

当前核心运行时以 LangGraph/ReAct assistant loop 为主，同时保留 mock/local/offline 路径用于稳定测试和演示。用户已确认后续本机项目运行主要使用真实 LLM；真实外部 Provider 仍必须通过 `provider_smoke` 或 `pilot` profile 和本机未跟踪配置显式启用。

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
- 工具调用必须经过 validator、executor、tool registry、policy/audit 相关边界；设计或修改工具调用链时按第 1 节使用对应项目 skill。
- Provider adapter 负责真实或 mock 能力接入；默认 profile 必须是 mock/local/offline。
- Memory 行为应通过 memory service/provider 管理，不把临时状态散落到无关模块；设计记忆服务时按第 1 节使用对应项目 skill。
- Memory tools 只是 Agent 可调用适配器，不是记忆服务所有者；检索、写入策略、TTL、去重、用户画像、审计和 store 选择必须留在 `MemoryManager`、`memory/` 或 `services/memory_*`。
- 多 agent / A2A 行为应通过 `assistant_agent.agent_routing` 聚合入口、AgentRouter、agent communication service、directory、transport adapter 和工具治理边界管理；设计 agent collaboration 时按第 1 节使用对应项目 skill。
- Gateway 是参考 `runTime` / OpenClaw 的外层业务入口，负责外部连接、通话生命周期、session/run/cancel/interrupt、用户级 session 复用和初始 agent 选择。AgentRouter 只负责内部 agent 选择、capability routing、controller/worker route 和控制面记录；面向调用方的多 agent 导入优先走 `assistant_agent.agent_routing`，底层实现仍保留在 `schemas/`、`services/` 等分层。
- 不要把 `runTime` 的旧 OpenClaw/Anthropic agent loop 引入本项目；当前 agent 内部执行器仍是 `AgentGraphRuntime` / assistant loop，`runtime` 一词优先留给这类内部执行器。
- API、demo、eval、CLI 应尽量复用同一套 runtime 行为，避免各自实现一套不一致的 Agent 逻辑。

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
| `src/assistant_agent/agent/` | LangGraph runtime、assistant loop、决策、验证、执行 | 按任务要求修改 |
| `src/assistant_agent/services/` | runtime services、context、trace、session、agent communication、provider 管理 | 按任务要求修改 |
| `src/assistant_agent/providers/` | Provider adapter、runtime profile、mock/real 边界 | 谨慎修改，默认 mock 优先 |
| `src/assistant_agent/tools/` | Tool registry、工具实现、策略、审计 | 按任务要求修改 |
| `src/assistant_agent/memory/` | 记忆服务、检索、存储 | 按任务要求修改 |
| `src/assistant_agent/eval/` | 离线评测逻辑 | 按任务要求修改 |
| `tests/` | pytest 测试 | 修改行为时同步维护，除非用户限制只读 |
| `scripts/` | 本地验证、服务、demo、eval、smoke 脚本 | 可按任务修改 |
| `docs/` | 当前权威文档、参考文档、历史归档 | 文档任务优先修改 |
| `docs/development/` | 历史开发计划、阶段记录和 runbook，暂不清理 | 仅追溯/清理任务按需阅读，不作为当前开发指导 |
| `docs/interview/` | 面试训练题库、解释材料、模拟面试规则 | 面试任务按 README 维护，默认不清理 |
| `.codex/skills/` | 项目内 Codex skills，包装专项工作流并路由到权威 docs | 只放项目专用 workflow，不复制长篇架构细节 |

如果用户对本轮任务设定更严格的 scope，例如“不要修改 `src/**`”或“不要修改 `tests/**`”，以用户当前约束为准。

## 7. 编码约定

- 新代码优先放入 `src/assistant_agent/` 的既有分层。
- 公共数据结构优先使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串。
- 外部模型/API 先维护 adapter interface 和 mock implementation，不要直接绑定具体供应商。
- 多 agent 通信先维护内部 message/task/artifact contract 和 transport adapter；A2A JSON-RPC 只作为协议适配层，不作为核心 runtime 内部模型。
- Memory tool 代码应保持薄层，只做 `ToolContext` 身份绑定、输入适配、调用 `MemoryManager`、包装 `ToolResult`；不要在 `tools/memory_tool.py` 新增检索排序、写入策略、画像合并、TTL、审计或直接 store 访问。
- Memory 工具选择采用 LLM-first：assistant loop 由 LLM 语义判断是否调用 `memory_save` / `memory_retrieval`，并在 `memory_save` 中声明 `source_intent`；当前不使用关键词或向量覆盖来源判断，任何写入仍必须经过 `MemoryWritePolicy`。
- Phase 8 之后的 assistant loop 方向是真实 LLM 自主决策、追问、工具调用和最终回答；不要让真实 LLM 路径依赖旧 intent/router/plan 来选择工具。
- mock/offline 路径只作为稳定测试与本地演示兼容层，不要把 mock 行为伪装成真实 LLM 能力。
- 新增核心 ReAct/assistant loop 测试应优先覆盖非 mock LLM 决策路径，例如 scripted/fake real chat adapter；真实外部网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 修改行为时同步更新相关测试和文档。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 8. 文档维护规则

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定，不塞入长篇历史设计。
- `README.md` 是临时占位入口，项目稳定后再重写，不承担实时架构和文档路由职责。
- `docs/tool-calling-architecture.md` 是 tool calling、ToolSpec、ActionValidator、ToolExecutor、ToolRegistry、provider-native tool calls 和 MCP `tool_run` 的当前权威入口。
- `docs/memory-service-architecture.md` 是记忆服务架构、边界、路由和更新规则的当前权威入口。
- `docs/memory-module-walkthrough.md` 是面向项目负责人的记忆模块中文解释文档；它不是 agent 必读或补读入口，维护时只需保持与 `docs/memory-service-architecture.md` 不冲突。
- `docs/development/memory-kernel-hardening-plan.md` 是 Memory Kernel 工程化落地的历史开发计划和参考材料，默认不作为当前设计权威入口。
- `docs/CONTEXT_ENGINEERING_STATUS.md` 是上下文工程当前进展、限制、下一步和新对话快速交接入口。
- `docs/context-engineering-walkthrough.md` 是面向项目负责人的上下文工程中文解释文档；它不是 agent 必读或补读入口，维护时只需保持与 `docs/CONTEXT_ENGINEERING_STATUS.md` 不冲突。
- `docs/development/context-engine-memory-policy-plan.md` 是已完成的上下文工程阶段实施记录，按需追溯历史，不是当前 active roadmap。
- `docs/agent-communication-routing.md` 是多 agent 实例、agent 通信路由、A2A adapter 边界和更新规则的当前权威入口。
- `docs/interview/README.md` 是面试训练模式、题库目录结构和更新规则的当前权威入口；各模块面试题按 `docs/interview/{module}_interview/` 独立维护。
- `.codex/skills/assistant-agent-tool-calling`、`.codex/skills/assistant-agent-context-engineering`、`.codex/skills/assistant-agent-memory-service`、`.codex/skills/assistant-agent-collaboration`、`.codex/skills/assistant-runtime-reference` 和 `.codex/skills/assistant-agent-interview-trainer` 是本项目的项目内 skills；它们只包装工作流和入口路由，不取代对应权威 docs，也不要当作通用全局 skill 维护。
- 历史 task/prompt/skill 构建材料和根目录通用文档已按用户确认删除；`docs/development/` 暂不清理。
- 新增文档必须有明确长期用途；优先更新 AGENTS 或现有专项文档，README 暂不承载实时维护内容。

## 9. 测试与验收

优先使用离线验证：

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
