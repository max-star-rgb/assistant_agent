# PostgreSQL Agent Server Store 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `127.0.0.1:8090` 启动使用独立 PostgreSQL 持久化的 Agent Server，并保存中文长期记忆。

**Architecture:** 使用官方 `langgraph build` 构建应用镜像，由项目 Compose 启动 Agent Server、PostgreSQL 与 Redis。LangMem 继续使用 Agent Server 注入的原生 Store，不引入项目自有 Store adapter；PostgreSQL named volume 负责跨容器重启持久化。

**Tech Stack:** LangGraph Agent Server、Docker Compose、PostgreSQL 16/pgvector、Redis 6、LangGraph SDK。

## Global Constraints

- 使用全新的 PostgreSQL，不迁移 `.langgraph_api`。
- 不复用或修改 Langfuse 容器、数据库、Redis 或 volume。
- API 只绑定 `127.0.0.1:8090`；PostgreSQL 与 Redis 不映射宿主端口。
- 不写入或提交 `.env`、LangSmith API key 或 Provider key。
- 不调用真实模型 Provider；只调用本地 Agent Server Store API。

---

### Task 1: 增加 PostgreSQL Compose 数据面

**Files:**
- Create: `deploy/agent_server/compose.yaml`
- Modify: `scripts/run_server.py`

**Interfaces:**
- Consumes: `langgraph.json`、本机 `.env`、Docker Engine。
- Produces: `assistant-agent-langgraph-api:local` 镜像和带 named volume 的 API/PostgreSQL/Redis 服务。

- [ ] **Step 1: 编写隔离 Compose**

  定义 `langgraph-api`、`langgraph-postgres`、`langgraph-redis`，API 使用 `POSTGRES_URI` 与 `REDIS_URI`，端口使用 `127.0.0.1:${ASSISTANT_AGENT_SERVER_PORT:-8090}:8000`。

- [ ] **Step 2: 扩展启动脚本**

  增加 `--backend dev|postgres` 和 `--rebuild`；postgres 模式在镜像缺失或显式 rebuild 时运行 `langgraph build`，随后以前台 Compose 启动。

- [ ] **Step 3: 静态验证 Compose**

  ```bash
  ASSISTANT_AGENT_SERVER_PORT=8090 docker compose -f deploy/agent_server/compose.yaml config
  ```

  确认只有 API 绑定宿主端口，且绑定地址为 `127.0.0.1`。

### Task 2: 同步当前 authority 与启动导航

**Files:**
- Modify: `docs/agent-server-architecture.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: Task 1 的启动入口和资源拓扑。
- Produces: PostgreSQL Store、Redis streaming 和本地 dev backend 的准确运维说明。

- [ ] **Step 1: 更新 Agent Server authority**

  记录 postgres 为持久化入口、`.langgraph_api` 只属于 dev backend，并明确 volume 与网络边界。

- [ ] **Step 2: 更新脚本命令**

  记录 `scripts/run_server.py --backend postgres --port 8090 --rebuild` 和后续不 rebuild 的启动方式。

- [ ] **Step 3: 校验文档 authority**

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py --repo-root .
  ```

### Task 3: 启动全新 PostgreSQL 并验证记忆

**Files:**
- Modify: Docker named volume 中的 PostgreSQL 数据。

**Interfaces:**
- Consumes: `http://127.0.0.1:8090` Store API。
- Produces: PostgreSQL 中唯一中文记忆“用户名字叫菜宝”。

- [ ] **Step 1: 停止旧 8090 dev server并构建启动**

  精确停止占用 `8090` 的旧 `langgraph dev`，运行 postgres backend 并等待 `/ok`。

- [ ] **Step 2: 写入中文记忆**

  在 LangMem namespace 写入 `{"kind":"Memory","content":{"content":"用户名字叫菜宝"}}`。

- [ ] **Step 3: 重启 API 与 PostgreSQL服务**

  使用 `docker compose restart`，不删除 volume。

- [ ] **Step 4: 验证跨重启恢复**

  Store API 必须返回唯一中文记忆；旧英文记忆匹配数必须为 0。

### Task 4: 最终验证与交付

**Files:**
- Review: `deploy/agent_server/compose.yaml`
- Review: `scripts/run_server.py`
- Review: `docs/agent-server-architecture.md`
- Review: `scripts/README.md`

**Interfaces:**
- Consumes: 代码、Compose、运行态与文档 diff。
- Produces: 验证报告和运行命令。

- [ ] **Step 1: 运行最小验证**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_gateway_contract.py tests/core/integration/test_memory_lifecycle.py
  git diff --check
  ```

- [ ] **Step 2: 汇报测试与 Provider 范围**

  明确本次未新增 pytest、未调用真实模型 Provider，并报告 PostgreSQL volume 与服务健康状态。
