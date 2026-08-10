# Langfuse 4.6 全新部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在生成并核验完整备份后，清空本机 Langfuse 3.224.2 运行数据，全新部署 Langfuse 4.6，并把仓库接入迁移到 v4 API。

**Architecture:** 本地 `.data/langfuse` 继续承载未跟踪的 Compose 与自定义 Web 镜像；新实例直接使用 v4 默认 `events_only` 数据模型，不启用 dual/legacy write。仓库中的 Experiment Score 审计改用 Observations v2，Remote Experiment 代理仅在 4.6 实测仍需要时保留。

**Tech Stack:** Docker Compose、Langfuse Server 4.6、Langfuse Python SDK 4.x、ClickHouse 25.12、PostgreSQL 17、Redis 7、MinIO、pytest。

## Global Constraints

- 删除动作仅允许命中 `langfuse_langfuse_postgres_data`、`langfuse_langfuse_clickhouse_data`、`langfuse_langfuse_clickhouse_logs`、`langfuse_langfuse_minio_data`、`langfuse_langfuse_redis_data`。
- 新备份核验成功前不得停止基础数据库或删除任何 volume；保留 `.data/backups/langfuse/20260729-104927/`。
- 不读取或输出 `.env` 中的凭据，不调用真实 LLM Provider，不运行付费 Agent/Judge eval。
- 保留用户当前对 `docs/observability-harness.md`、`scripts/README.md`、`src/assistant_agent/observability/runtime_audit/report.py` 和 `tests/tdd/runtime_audit/test_daily_runtime_audit.py` 的已有改动。
- 新实例直接使用 v4 默认数据模型，不设置 `LANGFUSE_MIGRATION_V4_WRITE_MODE`，不执行历史 backfill。

---

### Task 1: 创建并核验 Langfuse 3.224.2 完整备份

**Files:**
- Create: `.data/backups/langfuse/<timestamp>/manifest.sha256`
- Copy: `.data/backups/langfuse/<timestamp>/config/{.env,docker-compose.yml,docker-compose.override.yml,Dockerfile.langfuse-web,patch-additional-input-renderer.js}`
- Create: `.data/backups/langfuse/<timestamp>/{postgres-logical.dump,clickhouse-native.zip,redis-snapshot.rdb}`
- Create: `.data/backups/langfuse/<timestamp>/minio-logical/**`

**Interfaces:**
- Consumes: 当前健康的 `langfuse-*` Compose 服务和五个持久化 volumes。
- Produces: 可供 3.224.2 恢复的时间戳备份目录及 SHA-256 清单。

- [ ] **Step 1: 记录精确目标与当前状态**

Run:

```bash
docker compose -f .data/langfuse/docker-compose.yml ps --format json
docker volume ls --format '{{.Name}}' | rg '^langfuse_langfuse_(postgres|clickhouse|minio|redis)'
curl -fsS http://127.0.0.1:3000/api/public/health
```

Expected: health 返回 `3.224.2`，volume 名称只包含 Global Constraints 中列出的五项。

- [ ] **Step 2: 暂停写入端并创建备份目录**

Run:

```bash
docker compose -f .data/langfuse/docker-compose.yml stop langfuse-web langfuse-worker assistant-agent-eval-webhook
LANGFUSE_BACKUP_DIR=".data/backups/langfuse/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LANGFUSE_BACKUP_DIR/config" "$LANGFUSE_BACKUP_DIR/minio-logical"
```

Expected: 数据库与对象存储仍健康，Web/Worker/代理已停止；记录展开后的绝对备份路径供后续步骤使用。

- [ ] **Step 3: 生成四类数据备份和配置副本**

Run the PostgreSQL dump inside the container using its existing environment, create a ClickHouse native `File('langfuse-<timestamp>.zip')` backup and copy it from `/var/lib/clickhouse/backups/`, run `mc mirror --overwrite local/langfuse /tmp/minio-logical` followed by `docker cp`, and run Redis `SAVE` before copying `/data/dump.rdb`. Copy the five config files without printing their contents.

Expected: every command exits 0; PostgreSQL、ClickHouse、MinIO 和 Redis 产物均存在且非空。

- [ ] **Step 4: 生成清单并做恢复前置核验**

Run:

```bash
find "$LANGFUSE_BACKUP_DIR" -type f -size 0 -print
find "$LANGFUSE_BACKUP_DIR" -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum > "$LANGFUSE_BACKUP_DIR/manifest.sha256"
sha256sum --check "$LANGFUSE_BACKUP_DIR/manifest.sha256"
unzip -t "$LANGFUSE_BACKUP_DIR/clickhouse-native.zip"
/home/lenovo1/miniconda3/envs/hello_agent/bin/pg_restore --list "$LANGFUSE_BACKUP_DIR/postgres-logical.dump" >/dev/null
```

Expected: 零字节扫描无输出，所有 SHA-256 为 `OK`，ClickHouse zip 和 PostgreSQL dump 可读取，MinIO 目录至少包含一个对象。

---

### Task 2: 迁移 Experiment observation 定位到 v4 API

**Files:**
- Create: `tests/tdd/langfuse-v4-migration/test_v4_observation_lookup.py`
- Modify: `evals/agent/langfuse_backend.py:364`

**Interfaces:**
- Consumes: `Langfuse.api.observations.get_many(trace_id, name, type, limit)`。
- Produces: `verify_persisted_dimension_scores(client, result)` 只使用 Observations v2 定位 `experiment-item-task`。

- [ ] **Step 1: 写 RED 测试**

测试构造 fake client：`api.observations.get_many()` 返回唯一 observation；`api.legacy.observations_v1.get_many()` 一旦调用即抛错；`scores_v3.get_many_v3()` 返回三个 canonical Score，并断言 v2 调用参数包含 `trace_id`、`name="experiment-item-task"`、`type="SPAN"`、`limit=2`。

- [ ] **Step 2: 运行 RED 测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langfuse-v4-migration
```

Expected: FAIL，原因是实现仍访问 `api.legacy.observations_v1`。

- [ ] **Step 3: 实现最小 API 迁移**

在 `verify_persisted_dimension_scores()` 中把 observation 查询改为：

```python
observations_response = client.api.observations.get_many(
    trace_id=trace_id,
    name="experiment-item-task",
    type="SPAN",
    limit=2,
)
```

其余 Score v3 审计、重试和 observation ID 关联逻辑保持不变。

- [ ] **Step 4: 运行 GREEN 测试**

Run the same explicit pytest command.

Expected: PASS。该临时 TDD 目录保留，由用户决定是否整目录删除，不晋升 core。

---

### Task 3: 更新本地 4.6 Compose 与 Web 镜像

**Files:**
- Modify: `.data/langfuse/docker-compose.yml`
- Modify: `.data/langfuse/Dockerfile.langfuse-web`
- Possibly modify/delete: `.data/langfuse/patch-additional-input-renderer.js`

**Interfaces:**
- Consumes: 官方 `docker.io/langfuse/langfuse:4.6` 与 `docker.io/langfuse/langfuse-worker:4.6`。
- Produces: 直接使用 v4 默认数据模型的本地 Compose 配置。

- [ ] **Step 1: 更新镜像标签并静态校验**

将 Worker 更新为 `docker.io/langfuse/langfuse-worker:4.6`，Web build base 更新为 `docker.io/langfuse/langfuse:4.6`，本地 Web image 名更新为 `assistant-agent/langfuse-web:4.6-additional-input-pretty`。不得添加任何 v4 migration write-mode/backfill 环境变量。

Run:

```bash
docker compose -f .data/langfuse/docker-compose.yml config --images
```

Expected: Langfuse Web/Worker 均为 4.6，基础设施版本不变。

- [ ] **Step 2: 构建并判断 UI 补丁兼容性**

Run:

```bash
docker compose -f .data/langfuse/docker-compose.yml build --pull langfuse-web
```

Expected: 若旧 fragment 仍存在，补丁成功且构建通过；若补丁 fail-fast，检查 4.6 chunk 的 `Additional Input` renderer。新版已默认 pretty 时删除 patch build step；否则只更新 fragment 匹配并重新构建。

---

### Task 4: 清空旧 volumes 并启动全新 4.6

**Files:**
- Delete runtime data only: five exact Docker volumes in Global Constraints。

**Interfaces:**
- Consumes: Task 1 已核验备份和 Task 3 已构建镜像。
- Produces: 全新 Langfuse 4.6 实例。

- [ ] **Step 1: 再次验证删除目标**

列出 Compose project label 和每个 volume 名；若出现名称外目标或备份核验失败，立即停止，不执行删除。

- [ ] **Step 2: 删除精确 volumes**

Run:

```bash
docker compose -f .data/langfuse/docker-compose.yml down
docker volume rm langfuse_langfuse_postgres_data langfuse_langfuse_clickhouse_data langfuse_langfuse_clickhouse_logs langfuse_langfuse_minio_data langfuse_langfuse_redis_data
```

Expected: 只删除五个已核验目标；备份目录和其他 Docker volumes 不变。

- [ ] **Step 3: 启动并等待条件式健康检查**

Run:

```bash
docker compose -f .data/langfuse/docker-compose.yml up -d
```

轮询 Compose health 和 `http://127.0.0.1:3000/api/public/health`，最长 10 分钟，不使用固定长 sleep。

Expected: health payload 为 `status=OK`、`version=4.6`，所有依赖容器 healthy/running，Web/Worker 日志无 migration error。

---

### Task 5: 同步 v4 权威文档与项目 skill

**Files:**
- Modify: `evals/README.md`
- Modify: `scripts/README.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`

**Interfaces:**
- Consumes: Task 4 实测的 4.6 API、webhook payload/signature/port 行为。
- Produces: 不再依赖 `3.224.2` 或 legacy Observations API 的当前操作说明。

- [ ] **Step 1: 更新 observation 与数据模型说明**

把 `api.legacy.observations_v1` 改为 `api.observations` v2，说明本机 4.6 使用 v4 默认 `events_only`，Scores 仍由 Scores v3 API 审计。

- [ ] **Step 2: 实测后更新 Remote Experiment 说明**

使用本地 UI/HTTP 观察 4.6 webhook 的端口校验、payload 形态与 `x-langfuse-signature`。只有实测证明限制仍存在才保留相应版本化说明；保留代理时准确说明其当前职责，原生签名可用时移除“补签是必需”的旧结论。

- [ ] **Step 3: 合并用户已有文档改动**

编辑 `scripts/README.md` 前检查 `git diff -- scripts/README.md`，只修改 Langfuse 相关段落，不覆盖并发的 runtime audit 文档改动。

---

### Task 6: 完成离线与本地真实 Langfuse 验证

**Files:**
- Verify: `tests/tdd/langfuse-v4-migration/**`
- Verify: `evals/agent/**`
- Verify: local Langfuse instance and `.data/backups/langfuse/<timestamp>/**`

**Interfaces:**
- Consumes: 全新 4.6、v2 adapter、已同步文档。
- Produces: 可复核的升级结果与限制报告。

- [ ] **Step 1: 运行最小离线 pytest**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/langfuse-v4-migration
```

Expected: PASS；不运行真实 Provider。

- [ ] **Step 2: 验证本地接入路径**

使用本地测试 project 初始化数据，验证 OTLP trace 可见、`api.observations.get_many()` 可定位 observation、Scores v3 可写读、Dataset 可创建/读取。Remote Experiment 只验证 webhook 配置与代理传输，不启动真实 Agent/Judge eval。

- [ ] **Step 3: 最终核验**

Run `rg -n '3\\.224\\.2|api\\.legacy\\.observations_v1' evals/README.md scripts/README.md .codex/skills/langfuse-eval-engineering/SKILL.md evals/agent`，检查 Compose health/logs、备份 SHA-256 和 `git diff --check`。

Expected: 活动文档和实现无旧版本依赖，健康状态正常，备份仍完整。

- [ ] **Step 4: 汇报测试策略**

报告：

```text
Core invariant: unchanged.
Tests: added tests/tdd/langfuse-v4-migration for temporary RED/GREEN; user may delete the directory manually.
```

同时列出实际命令、备份绝对路径、删除的五个 volumes、Langfuse 4.6 健康状态、未调用真实 Provider，以及恢复 3.224.2 的入口。
