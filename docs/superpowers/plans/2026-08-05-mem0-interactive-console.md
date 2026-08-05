# Mem0 交互管理控制台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `scripts/run_mem0.py` 在 Mem0 健康后进入 PyCharm 友好的交互控制台，支持查看和直接管理全部原始记忆。

**Architecture:** 保留现有 Compose 启动与健康检查，在同一脚本内增加标准库 HTTP client、基于 `shlex` 的命令分派和容错 REPL。sidecar 为 Mem0 2.0.11 不支持的无身份全量 `get_all()` 提供 operator 读取分支，带身份 runtime recall 不变；不接入 runtime、ToolRegistry 或新建记忆协议。

**Tech Stack:** Python 3、标准库 `argparse` / `json` / `shlex` / `urllib`、Mem0 sidecar REST API。

## Global Constraints

- `add` 默认发送 `infer=false`；只有显式 `--infer` 才发送 `infer=true`。
- `list` 默认不携带身份过滤，显示所有用户原始记录。
- 不提供批量清空命令。
- `delete` 默认先展示目标并要求输入 `yes`；`--yes` 才跳过确认。
- 不新增依赖，不调用真实 Provider，不改动 runtime Memory 治理链。
- console 文案和 wrapper 不新增永久 pytest；sidecar 无身份列表 bugfix 使用独立 `tests/tdd/mem0-interactive-console/` 做临时 RED/GREEN。

---

### Task 1: 实现 Mem0 HTTP 管理客户端与交互命令

**Files:**
- Modify: `scripts/run_mem0.py`

**Interfaces:**
- Consumes: sidecar `GET /ready`、`GET /memories`、`GET /memories/{id}`、`GET /memories/{id}/history`、`POST /memories`、`PUT /memories/{id}`、`DELETE /memories/{id}`。
- Produces: `_interactive_console() -> int`、`_execute_command(args: list[str]) -> bool`、`_mem0_request(...) -> dict[str, object]`，以及各命令处理辅助函数。

- [ ] **Step 1: 扩展标准库导入和 HTTP 请求边界**

  增加 `argparse`、`shlex`、`urllib.parse`、`urllib.request.Request`、`HTTPError`，实现 JSON 请求辅助函数：仅连接 `http://127.0.0.1:8890`，支持 query/body、5 秒 timeout、响应必须为 JSON object，并把 HTTP 错误转成不含响应正文的可解释异常。

- [ ] **Step 2: 实现命令解析与 CRUD handler**

  使用独立 `ArgumentParser(add_help=False, exit_on_error=False)` 解析每个命令；实现 `status/list/get/history/add/update/delete/help`。`add` 缺少正文或所有身份字段时调用 `input()` 补齐，`update` 缺少正文时提示输入，`delete` 缺少 ID 时提示输入并在非 `--yes` 时读取目标、显示后确认。

- [ ] **Step 3: 实现容错 REPL 并接入启动主流程**

  健康检查成功后打印简短帮助并调用 `_interactive_console()`。空行忽略；`exit/quit` 返回；`EOFError` 正常退出；命令级 `KeyboardInterrupt`、参数错误和 HTTP 错误打印后继续。脚本退出时明确提示容器仍在运行以及 stop 命令。

- [ ] **Step 4: 执行无副作用的实现验证**

  运行：

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m py_compile scripts/run_mem0.py
  ```

  再使用临时进程内 harness 替换 `_mem0_request` 和 `input`，依次验证：空参数进入引导、默认 add payload 的 `infer` 为 `False`、`add --infer` 为 `True`、delete 输入非 `yes` 不发送 DELETE、未知命令报错后下一条 `help` 仍可执行。harness 不读取 `.env`、不启动 Docker、不访问网络、不写入真实记忆。

### Task 2: 同步 operator 使用说明

**Files:**
- Modify: `scripts/README.md`
- Modify: `docs/memory-service-architecture.md`

**Interfaces:**
- Consumes: Task 1 最终命令集与退出语义。
- Produces: PyCharm 和命令行操作者可直接使用的稳定入口说明。

- [ ] **Step 1: 更新脚本索引**

  将 `scripts/run_mem0.py` 的说明改为：健康后保持前台交互，列出 `help/list/get/history/add/update/delete/status/exit`，说明直接管理全部原始身份记录、`add` 默认不推理、`--infer` 显式启用推理、退出不停止容器。

- [ ] **Step 2: 更新 Memory 本地运行说明**

  在现有启动命令后补充相同的控制台行为与安全边界，明确这是 operator 控制台而非 Assistant memory tool/API，并说明不会提供批量清空命令。

- [ ] **Step 3: 检查差异与文档一致性**

  运行：

  ```bash
  git diff --check -- scripts/run_mem0.py scripts/README.md docs/memory-service-architecture.md
  rg -n "run_mem0|add --infer|delete" scripts/README.md docs/memory-service-architecture.md
  ```

  检查命令名、默认 `infer=false`、删除确认和退出后容器继续运行在代码与两份文档中一致。

### Task 2.5: 修复 Mem0 2.0.11 无身份全量列表

**Files:**
- Modify: `docker/mem0/mem0_env.py`
- Modify: `docker/mem0/mem0_sidecar.py`
- Modify: `docker/mem0/compose.yaml`
- Create: `tests/tdd/mem0-interactive-console/test_unfiltered_mem0_listing.py`

**Interfaces:**
- Consumes: Mem0 2.0.11 `_get_all_from_vector_store` 的原生结果格式化，以及现有 `collect_all_memories` 扩窗逻辑。
- Produces: `list_unfiltered_memories(mem0_memory, *, limit=None)`，只供 sidecar 无身份 operator 列表使用。

- [x] **Step 1: 写入并执行 RED 测试**

  显式运行临时 TDD，确认测试因 `list_unfiltered_memories` 不存在而失败。

- [x] **Step 2: 实现最小 GREEN 修复**

  无 filters 时通过 Mem0 vector-store formatter 读取并扩窗，包含原始身份记录；有 filters 时保持公开 `get_all()` 路径。Compose 只读挂载当前 sidecar 源码，使 `--no-build` 启动生效。

- [x] **Step 3: 验证 GREEN 与真实只读路径**

  临时 TDD 通过；重建本地 Mem0 容器配置后，`GET /ready` 和 `GET /memories?limit=1` 均返回 200。未执行真实写操作。

### Task 3: 最终验证与范围审计

**Files:**
- Inspect: `scripts/run_mem0.py`
- Inspect: `scripts/README.md`
- Inspect: `docs/memory-service-architecture.md`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的完整变更。
- Produces: 可复现的验证证据和不触碰用户已有改动的交付说明。

- [ ] **Step 1: 重跑最小验证**

  重跑 `py_compile`、受控 harness 和 `git diff --check`，记录实际结果。只有本机 sidecar 已健康时才额外执行只读 `GET /ready` 和 `GET /memories?limit=1` smoke；不得执行真实 add/update/delete。

- [ ] **Step 2: 审计工作区范围**

  使用 `git status --short` 和定向 `git diff --` 确认只修改本任务文件，不覆盖或回滚现有 Gateway、media、runtime 或其他 TDD 改动。

- [ ] **Step 3: 提交决策**

  当前工作区已有大量用户改动，且设计/计划文档默认不提交。本轮不自动提交；最终报告本任务文件、验证命令、Core invariant 与 Tests 决策，并说明未运行真实 Provider、未写入或删除真实记忆。
