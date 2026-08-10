# 服务启动运维摘要实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前分散且冗长的启动输出改造成由实际 app/runtime 状态生成的精简运维摘要，让管理员能快速判断服务是否就绪、是否降级以及当前安全暴露面。

**Architecture:** `run_server.py` 只把 launcher 的 bind、日志和调试开关写入结构化启动环境，不再提前宣称服务状态。FastAPI lifespan 完成 Runtime、Tool Registry 和后台 Worker 装配后，由 `server_startup_summary.py` 从实际对象及依赖探测结果构造统一报告并输出；完整 Tool 名称仅在显式 details 模式下展示。

**Tech Stack:** Python 3.11、FastAPI/Starlette、Pydantic Runtime 配置、现有 ToolRegistry 与 dependency probes。

## Global Constraints

- 默认 Provider 和验证使用 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，不得调用真实 Provider。
- 不新增依赖，不输出 secret、token、原始 Provider 响应或真实用户数据。
- 不根据工具名推断业务分类；工具统计只使用 `ToolSpec.category` 和注册来源 metadata。
- console/wrapper 文案不新增 pytest；使用导入检查和 mock 启动 smoke 验证。
- 不修改或回滚工作区中与本任务无关的已有改动。

---

### Task 1: 结构化启动报告

**Files:**
- Modify: `src/assistant_agent/runtime/server_startup_summary.py`
- Modify: `src/assistant_agent/runtime/startup_dependencies.py`

**Interfaces:**
- Consumes: `FastAPI.routes`、`runtime.config`、`runtime.registry`、app worker state、现有 `collect_startup_dependency_statuses()`。
- Produces: `build_server_startup_report(...) -> ServerStartupReport`、`format_server_startup_report(...) -> list[str]`、`print_server_startup_report(...) -> None`。

- [x] **Step 1: 明确结构化报告字段和状态规则**

  定义 `READY`、`READY (degraded)`；仅当已配置的可选依赖不可用时标记 degraded，disabled 项不进入默认输出。

- [x] **Step 2: 实现从 Runtime Registry 读取统计**

  使用 `registry.list_specs()` 按 `read/generate/write/dangerous` 统计，使用 registration record 统计来源数量；默认不输出 Tool 名称。

- [x] **Step 3: 实现入口、Worker、安全和日志摘要**

  从 app routes 统计 HTTP/WebSocket 入口，从 app state 读取 Worker 状态，从 launcher 环境读取 bind、日志级别和本地调试开关。

- [x] **Step 4: 保留显式详细 Tool 清单**

  复用现有 plugin ownership 分组，但仅在 details 开关开启时附加，标题明确为 `Tool sources`，不再冒充业务分类。

### Task 2: 接入真实应用生命周期

**Files:**
- Modify: `scripts/run_server.py`
- Modify: `src/assistant_agent/api/app.py`

**Interfaces:**
- Consumes: Task 1 的 `print_server_startup_report()`。
- Produces: launcher 启动阶段只打印 `STARTING`，app lifespan 完成关键组件装配后打印最终摘要。

- [x] **Step 1: 增加 `--startup-details` 并传递 launcher 元数据**

  将 host、port、console/file log level、日志路径和 details 开关写入 startup 环境，避免摘要函数重复解析 CLI 或硬编码地址。

- [x] **Step 2: 移除提前打印的 Provider/依赖/Service 清单**

  `uvicorn.run()` 前只输出一行 `assistant_agent STARTING` 与 bind 目标，不宣称 ready。

- [x] **Step 3: lifespan 装配完成后输出报告**

  在 durable task/workflow worker 启动完成后读取 app/runtime 实际状态并打印一次最终报告。

- [x] **Step 4: 执行最小离线验证**

  运行：

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q scripts/run_server.py src/assistant_agent/runtime/server_startup_summary.py src/assistant_agent/runtime/startup_dependencies.py src/assistant_agent/api/app.py
  ```

  再以 mock 模式短时启动服务，确认出现 `READY`、动态入口/工具统计、安全信息，且没有逐项 Tool dump；随后请求 `/health` 验证返回 `200`。

## Self-review

- 需求覆盖：动态 Runtime/App 事实、分类修正、默认降噪、健康与安全信息均有对应任务。
- Placeholder scan：无 TBD/TODO 或未决接口。
- Type consistency：报告构建、格式化和打印接口由 Task 1 定义，Task 2 只消费打印接口。
- Scope：只调整启动报告和装配时机，不扩展 readiness API 或修改 Runtime/Tool 治理语义。
