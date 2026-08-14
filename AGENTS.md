# AGENTS.md

本文件是 Codex / coding agent 的仓库级入口。开始仓库内任何非纯问答、非单条无副作用命令任务前，以本文件为准。README 只做人类快速导航；专项架构细节在 `docs/*.md` 权威文档中；源码和测试优先于过期 prose。

## 1. 项目与入口

项目名、发行名和 Python 包名均为 `assistant_agent`，源码在 `src/assistant_agent/`。默认 Python 使用本机 conda 环境 `hello_agent`，除非用户明确要求，不要重命名环境路径。

本项目是本地优先的助理 Agent。默认运行和 pytest 使用
`MULTIMODAL_AGENT_PROVIDER_MODE=mock`。只有 `evals/system` 的明确真实能力评审可以使用真实 Provider，并且必须通过
`MULTIMODAL_AGENT_PROVIDER_MODE=real`、本机未跟踪配置和对应 operator 确认开关显式启用。

`docs/authority.toml` 是全部 Agent-facing 当前文档的机器可读路由与 owner 清单。开始工程任务时：

1. 用明确任务类型匹配 `read_when`，用预计读取或修改的路径匹配 `source_globs`；
2. 先只读取匹配 domain 的 `authority`，并用文首 `Authority contract` 确认 owns / does not own；
3. 只有改动跨越 contract 边界时，才读取其中列出的相邻 authority；
4. 若文档与源码或测试不一致，以源码和测试为准，并在同次变更中回补 owner authority。

不要预读 manifest 中所有 authority。项目 skill 只作为 workflow 检查清单或脚本入口，不作为事实权威。
manifest 只用于 coding agent 选择工程文档，不进入产品 Runtime，不得用于判断终端用户意图、预选 Tool
或选择 workflow。

- 遇到 Provider 相关实现/调试时，优先联网核对官方文档，重点包括 DeepSeek tool calls（`https://api-docs.deepseek.com/zh-cn/guides/tool_calls`）、阿里百炼模型文档（`https://bailian.console.aliyun.com/cn-beijing/?spm=a2c4g.11186623.0.0.60393ba2UI7e5t&tab=doc#/doc/?type=model&url=2963787`）和火山引擎模型文档（`https://docs.volcengine.com/docs/82379/1099455?lang=zh`）。

`docs/development/**` 和 `docs/superpowers/**` 是开发阶段/历史材料，不作为当前规则或默认权威；`docs/interview/**` 只用于面试资料。只有用户点名、运行历史 runbook 或做对应历史/面试任务时才读。

## 2. 架构边界

硬边界：

- 入口层只负责接入和归一化请求；生产主运行时是 `native_agent.AssistantRootGraph`，fast 分支使用
  `create_agent`，planning 分支使用显式 StateGraph 并复用同一个 fast Agent。
- LangGraph Agent Server 负责 assistant/thread/run/checkpoint/cancel/interrupt/resume/stream 生命周期；媒体
  custom route 只做协议归一化与连接关联，不承担主大脑职责。
- 生产主链的本地显式工具调用使用标准 `BaseTool -> ToolNode` 与 `ToolRuntime` 注入；read Tool 使用官方
  retry middleware；fast 模式不触发 HITL，planning 模式对非 read Tool 使用原生 HITL；副作用幂等归具体 Tool 或业务 API。旧
  `ActionValidator -> ToolExecutor -> ToolRegistry -> tool` 只保留给尚未迁移的外围入口。配置为 `qwen` provider 的百炼兼容
  Chat Completions 通过显式配置启用的
  Provider-native 只读联网属于模型生成能力，不投影为本地 Tool，也不进入该执行链。
- Provider 运行只分 `mock` 和 `real`。mock 模式下主 LLM 与 Provider-backed tools 强制使用 mock；real 模式下主 LLM 必须完整配置，Provider-backed tools 只注册已完整配置的真实实现，禁止静默回退到 mock。
- Tool exposure 和入口路由不得用关键词、正则、高信号话术或手写请求规则推断用户意图；候选 Tool 由受信静态装配、MCP allowlist、entry/media/env 等结构化事实决定，具体调用与参数由 LLM 判断。
- 长期 Memory 读写只发生在父图固定的 `memory_recall` / `memory_commit` 节点；`memory_context` 是 checkpoint 冻结快照，后端只实现最小 `MemoryBackend` 协议，可接 LangMem、Mem0 或第三方服务。
- MCP、durable task、A2A、API、CLI、demo、eval 都是入口或调度形态；新入口应复用 Agent Server/native graph。尚未迁移的旧入口不得被描述为生产主链。
- 非 Python 的 Web UI、BFF、vendor adapter 或边缘入口只能做薄适配器；不要把旧 `runTime` agent loop 引入本项目。

## 3. 运行与安全

默认保持离线安全：

- 测试、eval、无 key 环境只走 mock/local/offline。
- 真实 Provider 只能在用户明确要求、`MULTIMODAL_AGENT_PROVIDER_MODE=real`、具体 Provider 显式配置同时满足时调用；不能因为检测到 key 自动启用。
- 不写入或提交 API key、token、真实 `.env`、真实用户数据、provider 原始响应、真实媒体、大文件、缓存或生成物。
- 允许主动在虚拟环境hello_agent中安装新依赖以完成开发目标
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
| `src/assistant_agent/` | 主源码；具体归属先用 `docs/authority.toml` 匹配 owner authority |
| `tests/core/` | 永久、默认收集的离线核心 pytest；只保护已登记 core invariant |
| `tests/tdd/*/` | 每个 feature 独立、显式运行且可由用户手动删除的临时 RED/GREEN pytest；不自动晋升 core |
| `evals/system/` | 正式真实能力验证，以及 `incubating/<feature>/` 中可删除的节点专项检查；边界与结果权威见 `evals/README.md` |
| `src/assistant_agent/evaluation/` | 原生 Graph evaluation target；当前没有 Release Review runner |
| `scripts/` | 服务、demo 和 system eval 的稳定命令入口；索引见 `scripts/README.md` |
| `docs/*.md` | 当前架构、接口和状态权威文档 |
| `docs/development/`, `docs/superpowers/`, `docs/interview/` | 非默认材料：开发阶段记录、历史计划/spec、面试资料；不作为当前规则入口 |
| `.codex/skills/` | 少量项目 workflow、检查清单和脚本；不作为事实权威 |

修改行为时按 `tests/README.md` 判断是否需要维护测试，并同步维护相关文档。若用户设定更严格 scope，以用户当前约束为准。

## 6. 开发规则

- 功能、代码设计优先拥抱原生langgraph。
- 新代码优先放入既有分层，公共契约优先使用 Pydantic model；不要为单次需求制造新架构。
- Tool、Provider、Memory、Agent Server、Context、多 Agent 和 durable task 的具体规则以 manifest 匹配的 authority 为准。
- 工具结果必须结构化，失败必须返回可解释错误；外部能力必须经过 adapter、mock/unconfigured 和安全 profile 边界。
- Memory tool、MCP、A2A、durable task 和入口层都保持薄适配，不把治理逻辑散落到入口脚本或 route 中。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 7. 文档与工作模式

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定；`README.md` 是人类轻导航入口。
- 当前 authority 只保留在 `docs/*.md`、`tests/README.md` 与 `evals/README.md`，并全部登记在
  `docs/authority.toml`。新增、删除或重命名 authority 时，同步更新 manifest、文首 contract card；仅当
  人类导航需要时再更新 README，不在 `AGENTS.md` 复制领域路由表。
- 普通开发默认不读 `docs/development/**`、`docs/superpowers/**`、`docs/interview/**`，除非用户点名或任务明确属于历史 runbook、历史设计记录或面试资料。
- 当用户基于真实测试、真实通话、真实 run/trace 或机器日志提问“为什么失败/为什么这样表现”，或提供 `assistant.turn: <trace_id>` 时，先按 `docs/observability-diagnosis-runbook.md` 读取对应机器事实，必要时再用 `docs/observability-harness.md` 核对观测契约，然后结合用户片段和源码回答。
- 执行中先读相关代码和文档，保持 scope 小；搜索优先用 `rg` / `rg --files`，手工编辑默认用 `apply_patch`。
- 修改当前 authority、`AGENTS.md` 文档路由、`docs/authority.toml` 或 authority validator 后，完成前运行
  `/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .`；
  `review_required` 只表示必须复核 owner，不要求机械制造文档 diff。
- 新增或修改 pytest、判断代码变更的验证范围、补充回归测试或诊断确定性测试失败时，使用 `.codex/skills/assistant-agent-development-testing`；该 skill 不指导功能实现。不得为小功能机械增加永久测试。
- 不回滚用户已有改动；提交时只包含本任务相关文件；新增设计文档默认不提交，除非用户明确要求纳入版本控制。
- 完成修改后需要判断是否应该提交本任务改动；Codex 处于计划模式时，完成后直接提交本任务改动；除非用户明确要求，否则不 push、不合并、不创建 PR。
- 结束任务时报告完成内容、验证结果、未完成/限制和下一步建议。

## 8. 测试导航

永久、默认 pytest 只有 `tests/core`；`tests/tdd/*/` 下的 feature 仅用于显式临时 RED/GREEN；
`evals/system/incubating/<feature>` 是可删除的节点专项区。是否新增测试、目录归属、最小验证范围和
`Core invariant:` / `Tests:` 汇报格式统一以 `tests/README.md` 为准。正式真实能力验证见
`evals/system`。当前没有上线前 Agent 行为质量 runner；后续重建必须直接消费原生 Graph，不得用 mock
fallback、路径混放、旧 Runtime facade 或重复 runner 伪装成真实评审。`AGENTS.md` 只提供入口，不复制具体规则。

## 9. 业务专项

涉及好单库相关功能时，先读取：

- `haodanku-openapi-docs/AI使用说明.md`
- `haodanku-openapi-docs/接口目录.md`

再按意图路由到对应分类文档编码。不要跳过本地 validator、executor、policy、audit 边界直接调用外部服务。
