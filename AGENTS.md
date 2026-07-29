# AGENTS.md

本文件是 Codex / coding agent 的仓库级入口。开始仓库内任何非纯问答、非单条无副作用命令任务前，以本文件为准。README 只做人类快速导航；专项架构细节在 `docs/*.md` 权威文档中；源码和测试优先于过期 prose。

## 1. 项目与入口

项目名、发行名和 Python 包名均为 `assistant_agent`，源码在 `src/assistant_agent/`。默认 Python 使用本机 conda 环境 `hello_agent`，除非用户明确要求，不要重命名环境路径。

本项目是本地优先的助理 Agent。默认运行和 pytest 使用
`MULTIMODAL_AGENT_PROVIDER_MODE=mock`。只有 `evals/system` 或明确的 `evals/agent`
真实 Task eval 可以使用真实 Provider，并且必须通过
`MULTIMODAL_AGENT_PROVIDER_MODE=real`、本机未跟踪配置和对应 operator 确认开关显式启用。

开始任务时，先按任务类型读取对应 `docs/*.md` 权威文档；如果文档与当前源码不一致，以源码和测试为准，并在本次变更中回补文档。项目 skill 只作为 workflow 检查清单或脚本入口，不作为事实权威。

| task | read first |
| --- | --- |
| Gateway、realtime、WebSocket、Media-Agent | `docs/gateway-architecture.md`；`docs/media-agent-service-websocket.md` |
| assistant loop、runtime stream、provider stream | `docs/runtime-event-stream-architecture.md` |
| tool calling、MCP、durable task、provider 调用治理 | `docs/tool-calling-architecture.md` |
| memory、本地/外部记忆服务、记忆读写策略 | `docs/memory-service-architecture.md`；`docs/memory_server_api_spec.md` |
| context、prompt、conversation history、context budget | `docs/CONTEXT_ENGINEERING_STATUS.md` |
| 长时 Agent、durable task 定时恢复、proactive wake、任务提醒 | 先读 `docs/gateway-architecture.md`、`docs/runtime-event-stream-architecture.md`、`docs/tool-calling-architecture.md`，再读开发路线图 `docs/development/2026-07-25-long-running-agent-plan.md`；路线图不是当前事实权威 |
| multi-agent、A2A、delegation | `docs/agent-communication-routing.md` |
| trace、observability、redaction、`assistant.turn` / Langfuse trace 定位与真实运行诊断 | `docs/observability-harness.md` |
| pytest 分层、目录归属、默认收集和新增测试规则 | `tests/README.md` |
| system eval、Agent task eval、Langfuse Experiment、真实 Provider 评测运行 | `evals/README.md` |

- 遇到 Provider 相关实现/调试时，优先联网核对官方文档，重点包括 DeepSeek tool calls（`https://api-docs.deepseek.com/zh-cn/guides/tool_calls`）、阿里百炼模型文档（`https://bailian.console.aliyun.com/cn-beijing/?spm=a2c4g.11186623.0.0.60393ba2UI7e5t&tab=doc#/doc/?type=model&url=2963787`）和火山引擎模型文档（`https://docs.volcengine.com/docs/82379/1099455?lang=zh`）。

`docs/development/**` 和 `docs/superpowers/**` 是开发阶段/历史材料，不作为当前规则或默认权威；`docs/interview/**` 只用于面试资料。只有用户点名、运行历史 runbook 或做对应历史/面试任务时才读。

## 2. 架构边界

硬边界：

- 入口层只负责接入和归一化请求；主运行时仍是 `AgentGraphRuntime` / assistant loop。
- Gateway 负责 session/run/cancel/interrupt/reconnect/stream frame 生命周期，不承担主大脑职责。
- 所有本地显式工具调用和外部副作用必须经过
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。配置为 `qwen` provider 的百炼兼容
  Chat Completions 固定启用的
  Provider-native 只读联网属于模型生成能力，不投影为本地 Tool，也不进入该执行链。
- Provider 运行只分 `mock` 和 `real`。mock 模式下主 LLM 与 Provider-backed tools 强制使用 mock；real 模式下主 LLM 必须完整配置，Provider-backed tools 只注册已完整配置的真实实现，禁止静默回退到 mock。
- Tool catalog、tool exposure、工具预选和入口路由不得用关键词、正则、高信号话术或手写请求规则推断用户意图；只能基于 `ToolSpec` policy/category、代码配置、结构化显式 opt-in、entry profile、media/env 等结构化事实定义候选工具空间。是否调用候选工具、调用哪个工具和如何构造参数由 LLM 判断；执行阶段仍必须做安全、授权、幂等和 schema 校验。
- Memory 读写必须经过 `MemoryManager`、read/write policy、store/audit 边界；memory tool 保持薄适配。
- MCP、durable task、A2A、API、CLI、demo、eval 都是入口或调度形态，不能绕过 runtime、tool、provider、memory 治理链路。
- API、demo、eval、CLI 应复用同一套 runtime 行为，避免各自实现 Agent 逻辑。
- 非 Python 的 Web UI、BFF、vendor adapter 或边缘入口只能做薄适配器；不要把旧 `runTime` agent loop 引入本项目。

## 3. 运行与安全

默认保持离线安全：

- 测试、eval、无 key 环境只走 mock/local/offline。
- 真实 Provider 只能在用户明确要求、`MULTIMODAL_AGENT_PROVIDER_MODE=real`、具体 Provider 显式配置同时满足时调用；不能因为检测到 key 自动启用。
- 不写入或提交 API key、token、真实 `.env`、真实用户数据、provider 原始响应、真实媒体、大文件、缓存或生成物。
- 不主动安装新依赖、不联网拉取依赖，除非用户明确要求并允许；需要安装时先询问用户。
- 如果本轮调用了真实 Provider，最终报告必须说明调用范围和验证结果。

## 4. 本地命令

默认使用：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python
```

测试与 eval 的职责边界见第 8 节；具体命令和运行约束以 `tests/README.md`、
`evals/README.md`、`scripts/README.md` 或对应 `docs/*.md` 为准。历史 runbook
只有用户点名时才读取。只有在需要 conda 激活环境变量时才使用
`conda run -n hello_agent <command>`。

## 5. 目录导航

| path | responsibility |
| --- | --- |
| `src/assistant_agent/` | 主源码；具体归属先看第 1 节任务路由和对应架构文档 |
| `tests/` | 全部离线 pytest；按 `unit/integration/contract` 和稳定故障域组织，规则以 `tests/README.md` 为唯一权威 |
| `evals/system/` | operator 显式触发的真实能力验证；手写 Python runner、结构化硬断言和 `.data/evals/system/` artifact 是结果权威 |
| `evals/agent/` | Task 中心的端到端 Agent 行为评估；Git 定义 Task/Environment/Grader，Langfuse 保存 Dataset、Experiment、Trace 和 Score |
| `scripts/` | 服务、demo、system eval 和 Agent task eval 的稳定命令入口；索引见 `scripts/README.md` |
| `docs/*.md` | 当前架构、接口和状态权威文档 |
| `docs/development/`, `docs/superpowers/`, `docs/interview/` | 非默认材料：开发阶段记录、历史计划/spec、面试资料；不作为当前规则入口 |
| `.codex/skills/` | 少量项目 workflow、检查清单和脚本；不作为事实权威 |

修改行为时按 `tests/README.md` 判断是否需要维护测试，并同步维护相关文档。若用户设定更严格 scope，以用户当前约束为准。

## 6. 开发规则

- 新代码优先放入既有分层，公共契约优先使用 Pydantic model；不要为单次需求制造新架构。
- Tool、Provider、Memory、Gateway、Context、多 Agent 和 durable task 的具体规则以第 1 节对应权威文档为准。
- 工具结果必须结构化，失败必须返回可解释错误；外部能力必须经过 adapter、mock/unconfigured 和安全 profile 边界。
- Memory tool、MCP、A2A、durable task 和入口层都保持薄适配，不把治理逻辑散落到入口脚本或 route 中。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 7. 文档与工作模式

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定；`README.md` 是人类轻导航入口。
- 当前架构权威文档只保留在 `docs/*.md`；新增、删除或重命名 root authority 时，同步更新第 1 节路由表和 README。
- 普通开发默认不读 `docs/development/**`、`docs/superpowers/**`、`docs/interview/**`，除非用户点名或任务明确属于历史 runbook、历史设计记录或面试资料。
- 当用户基于真实测试、真实通话、真实 run/trace 或机器日志提问“为什么失败/为什么这样表现”，或提供 `assistant.turn: <trace_id>` 时，先按 `docs/observability-harness.md` 的真实运行定位与诊断规则读取对应机器事实，再结合用户片段和源码回答。
- 执行中先读相关代码和文档，保持 scope 小；搜索优先用 `rg` / `rg --files`，手工编辑默认用 `apply_patch`。
- 新增或修改 pytest、判断代码变更的验证范围、补充回归测试或诊断确定性测试失败时，使用 `.codex/skills/assistant-agent-development-testing`；该 skill 不指导功能实现，只有窄层无法证明 wiring 时才增加离线跨层验收。
- 不回滚用户已有改动；提交时只包含本任务相关文件；新增设计文档默认不提交，除非用户明确要求纳入版本控制。
- 完成修改后需要判断是否应该提交本任务改动；Codex 处于计划模式时，完成后直接提交本任务改动；除非用户明确要求，否则不 push、不合并、不创建 PR。
- 结束任务时报告完成内容、验证结果、未完成/限制和下一步建议。

## 8. 测试导航

本项目采用风险驱动测试。是否新增测试、测试文件如何组织、最小充分验证范围和任务结束时的
`Tests:` 汇报格式，统一以 `tests/README.md` 为准。pytest 只回答确定性代码契约是否正确；
`evals/system` 回答真实 Tool、Context 或 Memory 能力是否连通；`evals/agent`
回答 Agent 在端到端 Task 上的行为质量。三者不得用 mock fallback、路径混放或重复 runner
伪装成彼此。`AGENTS.md` 只提供入口，不复制具体测试与评分规则。

## 9. 业务专项

涉及好单库相关功能时，先读取：

- `haodanku-openapi-docs/AI使用说明.md`
- `haodanku-openapi-docs/接口目录.md`

再按意图路由到对应分类文档编码。不要跳过本地 validator、executor、policy、audit 边界直接调用外部服务。
