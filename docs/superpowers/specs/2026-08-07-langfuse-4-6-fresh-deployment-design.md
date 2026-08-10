# Langfuse 4.6 全新部署设计

## 目标

将本机自托管 Langfuse Server 从 `3.224.2` 升级到 `4.6`。现有 Langfuse 业务数据无需迁移到新实例，
但清空前必须生成可恢复、可核验的完整备份。新实例直接使用 v4 默认数据模型，不启用 legacy/dual
write，也不执行历史 backfill。

## 范围

- 备份当前 PostgreSQL、ClickHouse、MinIO 数据及不含凭据内容展示的本地 Compose 配置。
- 停止 Langfuse Compose，删除且仅删除该 Compose 项目的 PostgreSQL、ClickHouse、MinIO、Redis
  运行 volumes。
- 将 Web、Worker 和本地自定义 Web 镜像更新到 `4.6`，保持现有网络、安全配置和 Remote Experiment
  代理边界。
- 检查 `Additional Input` 默认 pretty-view 补丁；新版已原生满足需求时移除补丁，否则适配并保留。
- 将仓库中依赖 v3 write mode 的 Observations legacy API 调用迁移到 v4 Observations v2，并同步
  `evals/README.md`、`scripts/README.md` 和项目 Langfuse workflow 说明。

## 数据安全与恢复

升级前创建新的 `.data/backups/langfuse/<timestamp>/`，至少包含 PostgreSQL 逻辑备份、ClickHouse
原生备份、MinIO 对象备份和 Compose 配置副本。备份命令必须成功，且通过文件存在性、非零大小和
备份清单核验后，才允许停止服务和删除 volumes。保留现有
`.data/backups/langfuse/20260729-104927/`，不覆盖、不删除任何历史备份。

若 4.6 启动或兼容验证失败，停止新实例；可用备份重新建立 3.224.2 实例并恢复数据。恢复演练不在
本轮默认范围内，但最终报告提供恢复入口和备份位置。

## 实施与验证

1. 记录当前容器、镜像、volume 和健康状态，生成并核验新备份。
2. 更新 Compose、自定义镜像、代码和权威文档；先做静态配置检查。
3. 停止旧实例，精确删除 `langfuse` Compose 项目的四类数据 volumes，启动全新 4.6。
4. 验证健康接口返回 4.6、所有容器健康、schema migration 无错误，并检查 Web UI。
5. 使用本地 Langfuse 验证 OTLP trace、Observations v2、Score、Dataset 和 Remote Experiment webhook
   的基础连通性；不调用真实 LLM Provider，不运行付费 Agent/Judge eval。
6. 运行与 Langfuse adapter/评测基础设施相关的最小离线 pytest；仅在影响无法界定时扩大范围。

## 成功标准

- 新备份已核验并保留；旧备份未改变。
- 本机健康接口报告 Langfuse `4.6`，运行数据来自全新 volumes。
- v4 API 和本项目 Langfuse 接入路径通过验证，不再依赖 v3 write mode 的 legacy Observations API。
- 文档不再声称本机固定使用 `3.224.2`，并准确描述 v4 默认数据模型与本地 webhook 行为。
