# Multimodal Agent

本仓库实现一个本地优先的多模态自主工具调用 Agent。当前项目已经从早期阶段任务推进进入成型状态：核心运行时以 LangGraph/ReAct assistant loop 为主，默认使用 mock/local/offline provider，用于稳定开发、测试和演示；真实外部 Provider 只允许在显式 opt-in 的 smoke/pilot 场景中调用。

## Current Status

- API 层使用 FastAPI，提供健康检查、Agent 调用、记忆、评测和演示相关接口。
- Agent 层以 `AgentGraphRuntime` 和 assistant loop 为核心，负责意图理解、追问、工具选择、工具结果融合和最终回答。
- 工具调用通过 `AssistantDecision -> ActionValidator -> ToolExecutor -> ToolRegistry -> Tool` 边界执行，不应绕过 validator、executor、policy 或 audit。
- Provider 默认走 mock/local/offline 路径。API key 只用于显式 opt-in 的真实 Provider smoke/pilot，不会因为本地存在 key 自动启用真实调用。
- Memory、demo、eval、CLI 和 Web UI 均围绕同一套本地优先运行时组织。

## Quick Start

本仓库默认使用 conda 环境 `hello_agent`。建议直接调用该环境解释器：

```bash
PY=/home/lenovo1/miniconda3/envs/hello_agent/bin/python

$PY scripts/check_env.py
$PY -m pytest
$PY scripts/run_evals.py
$PY scripts/run_demo_flows.py
```

本地离线直跑：

```bash
$PY scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
```

启动本地 mock 服务：

```bash
$PY scripts/run_server.py --provider mock --image-provider mock
```

调用 CLI 客户端：

```bash
$PY scripts/run_client.py --server http://127.0.0.1:8000 "你好，帮我总结一下当前能力"
```

常用本地 URL：

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/demo/console
```

## Architecture

当前主流程：

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
        |
        v
structured observations -> final answer / events / audit logs
```

关键目录：

| path | responsibility |
| --- | --- |
| `src/multimodal_agent/api/` | FastAPI app、routes、server/client integration |
| `src/multimodal_agent/agent/` | LangGraph runtime、assistant loop、决策、验证、执行、事件 |
| `src/multimodal_agent/providers/` | LLM、图片、视频等 provider adapter 与 profile 配置 |
| `src/multimodal_agent/tools/` | Tool registry、工具实现、工具策略和审计边界 |
| `src/multimodal_agent/memory/` | 会话记忆、检索、存储、memory provider |
| `src/multimodal_agent/eval/` | 离线评测用例、评测 runner 和报告结构 |
| `scripts/` | 环境检查、服务启动、CLI、demo、eval、smoke 脚本 |
| `tests/` | pytest 测试，覆盖 unit/integration/API/demo/eval 等路径 |
| `docs/` | 当前权威文档、架构文档、API 文档、历史归档 |
| `tasks/` | 历史阶段任务和仍可参考的执行记录 |

## Runtime Modes

默认规则：

- 默认使用 mock/local/offline provider。
- 不自动调用真实 LLM、图片、视频、商品、通知或其他外部 Provider。
- 不把 API key、token、真实 `.env`、真实用户数据、真实 provider raw response 写入仓库。
- 不提交真实媒体、生成物、大文件或外部服务返回原文。

真实 Provider 仅限显式 opt-in：

- 需要任务明确要求真实 Provider smoke/pilot。
- 需要使用受控 runtime profile，例如 `provider_smoke` 或 `pilot`。
- 需要本地环境变量提供 key，且不能写入仓库。
- 测试和 demo 默认仍应走 mock/local/offline。

## Common Tasks

| 要做什么 | 先看哪里 | 可改哪里 | 验收命令 |
| --- | --- | --- | --- |
| 理解当前项目 | `docs/CODEX_PROJECT_GUIDE.md`、`docs/DOCS_INDEX.md` | 通常不需要改文件 | `git diff --name-status` |
| 修改文档 | `README.md`、`AGENTS.md`、`docs/CODEX_PROJECT_GUIDE.md` | `docs/**`、根目录入口文档 | `python scripts/check_env.py` |
| 新增 demo 场景 | `scripts/run_demo_flows.py`、`docs/demo-flows.md` | `scripts/**`、`docs/**`、必要时测试 | `python scripts/run_demo_flows.py` |
| 调整 provider mock | `docs/configuration.md`、`docs/provider-setup.md`、`src/multimodal_agent/providers/` | 按任务范围修改 provider/mock 与测试 | `python -m pytest` |
| 调整 memory 行为 | `docs/phase8/memory-manager-boundary.md`、`src/multimodal_agent/memory/` | memory 模块和相关测试 | `python -m pytest tests` |
| 更新 eval | `scripts/run_evals.py`、`tests/evals/eval_cases.json`、`docs/development.md` | eval 用例、脚本、文档 | `python scripts/run_evals.py` |
| 更新 API 文档 | `docs/quickstart.md`、`docs/observability-local.md`、API routes | `docs/**`，必要时 API 测试 | `python -m pytest tests` |

## Documentation

当前入口文档：

- `README.md`：人类读者入口，说明项目定位、架构、运行方式和常用命令。
- `AGENTS.md`：Codex / coding agent 的仓库级行为约束。
- `docs/CODEX_PROJECT_GUIDE.md`：后续 Codex 快速理解当前项目的权威项目指南。
- `docs/DOCS_INDEX.md`：文档清单和历史文档状态索引。
- `docs/TESTS_REVIEW.md`：tests 目录的只读评估和后续整理建议。

用户向文档：

- [Quickstart](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Capabilities](docs/capabilities.md)
- [Configuration](docs/configuration.md)
- [Provider Setup](docs/provider-setup.md)
- [Demo Flows](docs/demo-flows.md)
- [Local Deployment](docs/deployment-local.md)
- [Development](docs/development.md)
- [Security](docs/security.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release Checklist](docs/release-checklist.md)

历史 phase/task/skill/prompt 文档默认保留或归档，不直接删除。Historical phase docs remain available for traceability, but ordinary users should not need to read them. 删除文档前应先在 `docs/DOCS_INDEX.md` 标为 `delete-candidate`，写明重复、过期和已吸收位置，并经过人工确认。

## Validation

优先使用本地离线命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

如果环境安装了 lint/type 工具，也可以运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff format --check .
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m mypy src
```

当前仓库的安全默认值是离线验证。不要为了验证文档或普通开发任务调用真实 Provider。
