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

## 5. 目录导航

| path | responsibility |
| --- | --- |
| `src/assistant_agent/` | 主源码；具体归属先看第 1 节任务路由和 `tests/scope-map.toml` |
| `tests/`, `scripts/` | 测试、验证、服务、demo、eval、smoke 入口；测试分层以 `tests/README.md` 为准 |
| `docs/*.md` | 当前架构、接口和状态权威文档 |
| `docs/development/`, `docs/superpowers/`, `docs/interview/` | 非默认权威材料：runbook、历史计划/spec、面试资料 |
| `.codex/skills/` | 少量项目 workflow、检查清单和脚本；不作为事实权威 |

修改行为时同步维护相关测试和文档。若用户设定更严格 scope，以用户当前约束为准。

## 6. 开发规则

- 新代码优先放入既有分层，公共契约优先使用 Pydantic model；不要为单次需求制造新架构。
- Tool、Provider、Memory、Gateway、Context、多 Agent 和 durable task 的具体规则以第 1 节对应权威文档为准。
- 工具结果必须结构化，失败必须返回可解释错误；外部能力必须经过 adapter、mock/unconfigured 和安全 profile 边界。
- Memory tool、MCP、A2A、durable task 和入口层都保持薄适配，不把治理逻辑散落到入口脚本或 route 中。
- 业务功能建议从具体模块导入；只有明确包级公共入口才放进 `__init__.py` 聚合导出。

## 7. 文档与工作模式

- `AGENTS.md` 是当前唯一 agent 工作入口，应简短稳定；`README.md` 是人类轻导航入口。
- 当前架构权威文档只保留在 `docs/*.md`；新增、删除或重命名 root authority 时，同步更新第 1 节路由表和 README。
- 普通开发默认不读 `docs/development/**`、`docs/superpowers/**`、`docs/interview/**`，除非用户点名或任务属于这些材料。
- 执行中先读相关代码和文档，保持 scope 小；搜索优先用 `rg` / `rg --files`，手工编辑默认用 `apply_patch`。
- 不回滚用户已有改动；提交时只包含本任务相关文件，除非用户明确要求，否则不 push、不合并、不创建 PR。
- 结束任务时报告完成内容、验证结果、未完成/限制和下一步建议。

## 8. 业务专项

涉及好单库相关功能时，先读取：

- `haodanku-openapi-docs/AI使用说明.md`
- `haodanku-openapi-docs/接口目录.md`

再按意图路由到对应分类文档编码。不要跳过本地 validator、executor、policy、audit 边界直接调用外部服务。
