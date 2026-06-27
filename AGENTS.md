# AGENTS.md

本文件是给 Codex / coding agent 的仓库级指导。它应保持稳定、可自动加载，并只记录当前项目的通用规则。详细架构、文档清单和测试评估在 `docs/`。

## 1. 当前权威入口

开始任何工作前，优先阅读：

1. `docs/CODEX_PROJECT_GUIDE.md`：当前项目架构、运行边界和 Codex 工作入口。
2. `docs/DOCS_INDEX.md`：文档状态清单，判断哪些文档是 canonical/reference/historical/archive-candidate。
3. `README.md`：人类入口和常用运行命令。
4. `docs/architecture-layers.md`：涉及架构分层、模块归属、治理边界或重构判断时必须阅读。

历史 `tasks/`、`prompts/`、`skills/`、phase 文档只在用户明确点名、需要追溯历史决策或执行对应历史任务时阅读。不要把旧 roadmap 当成当前真实架构。

## 2. 项目定位

本仓库实现一个本地优先的多模态自主工具调用 Agent。Agent 负责理解用户输入、选择工具、执行受控调用、融合结果并给出最终回答；具体能力由工具、provider adapter、memory service、demo/eval/API 层协作提供。

当前核心运行时以 LangGraph/ReAct assistant loop 为主，同时保留 mock/local/offline 路径用于稳定测试和演示。真实外部 Provider 是显式 opt-in 能力，不是默认运行路径。

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
- API、demo、eval、CLI 应尽量复用同一套 runtime 行为，避免各自实现一套不一致的 Agent 逻辑。

## 4. 运行与安全规则

默认规则不可随意放宽：

- 默认只允许 mock/local/offline 路径。
- 不自动调用真实 LLM、图片、视频、商品、通知、数据库或其他外部 Provider。
- 不因为检测到 API key 就启用真实 Provider。
- 不写入 API key、token、真实 `.env`、真实用户数据或真实 provider raw response。
- 不提交真实媒体、生成物、大文件、缓存目录或外部服务原始返回。
- 不安装新依赖，不联网拉取依赖，除非用户明确要求并允许。

真实 Provider 调用必须同时满足：

- 用户或任务明确要求真实 Provider smoke/pilot。
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
| `src/multimodal_agent/api/` | FastAPI app、routes、server/client integration | 按任务要求修改 |
| `src/multimodal_agent/agent/` | LangGraph runtime、assistant loop、决策、验证、执行 | 按任务要求修改 |
| `src/multimodal_agent/providers/` | Provider adapter、runtime profile、mock/real 边界 | 谨慎修改，默认 mock 优先 |
| `src/multimodal_agent/tools/` | Tool registry、工具实现、策略、审计 | 按任务要求修改 |
| `src/multimodal_agent/memory/` | 记忆服务、检索、存储 | 按任务要求修改 |
| `src/multimodal_agent/eval/` | 离线评测逻辑 | 按任务要求修改 |
| `tests/` | pytest 测试 | 修改行为时同步维护，除非用户限制只读 |
| `scripts/` | 本地验证、服务、demo、eval、smoke 脚本 | 可按任务修改 |
| `docs/` | 当前权威文档、参考文档、历史归档 | 文档任务优先修改 |
| `tasks/` | 历史或阶段任务说明 | 默认参考或归档，不直接删除 |
| `prompts/` | 历史 prompt 或任务 prompt | 默认参考或归档，不直接删除 |
| `skills/` | 历史 skill 定义或 agent 工作流 | 默认参考或归档，不直接删除 |

如果用户对本轮任务设定更严格的 scope，例如“不要修改 `src/**`”或“不要修改 `tests/**`”，以用户当前约束为准。

## 7. 编码约定

- 新代码优先放入 `src/multimodal_agent/` 的既有分层。
- 公共数据结构优先使用 Pydantic model。
- 工具调用结果必须结构化，不允许只返回散乱字符串。
- 外部模型/API 先维护 adapter interface 和 mock implementation，不要直接绑定具体供应商。
- Phase 8 之后的 assistant loop 方向是真实 LLM 自主决策、追问、工具调用和最终回答；不要让真实 LLM 路径依赖旧 intent/router/plan 来选择工具。
- mock/offline 路径只作为稳定测试与本地演示兼容层，不要把 mock 行为伪装成真实 LLM 能力。
- 新增核心 ReAct/assistant loop 测试应优先覆盖非 mock LLM 决策路径，例如 scripted/fake real chat adapter；真实外部网络调用只放在显式 opt-in 的 smoke/integration 测试中。
- 工具执行失败必须返回可解释错误，不能直接抛出未处理异常给 Agent。
- 修改行为时同步更新相关测试和文档。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 8. 文档维护规则

- `README.md` 是人类入口，说明项目当前定位、架构、运行方式和常用命令。
- `AGENTS.md` 是 agent 行为约束入口，应简短稳定，不塞入长篇历史设计。
- `docs/CODEX_PROJECT_GUIDE.md` 是 Codex 快速理解当前项目的权威指南。
- `docs/DOCS_INDEX.md` 是文档清单和清理依据。
- `docs/TESTS_REVIEW.md` 是 tests 目录只读评估入口。
- 历史 phase/task/skill/prompt 文档默认保留或归档，不直接删除。
- 删除文档必须先进入 `delete-candidate`，写明重复、过期、已吸收位置，并经过人工确认。

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
