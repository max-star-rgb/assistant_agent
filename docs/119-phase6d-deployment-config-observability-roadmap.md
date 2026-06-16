# 119 Phase 6D：Local Deployment / Config / Observability

## 目标

让项目能被稳定地本地部署、配置和调试。

## 核心产物

```text
.env.example 完整化
Dockerfile
docker-compose.yml
docs/configuration.md
docs/deployment-local.md
healthcheck
logging / trace 说明
```

## 推荐本地部署

```bash
docker compose up
```

或：

```bash
uvicorn multimodal_agent.api.app:app --reload
```

## Observability

最小可观测性：

```text
run_id
trace_id
tool_calls
provider_errors
budget errors
memory operations
```

## 不做什么

- 不做 Kubernetes。
- 不做云部署。
- 不做生产级 Prometheus/Grafana。
- 不做复杂队列。
- 不做多用户权限系统。

## 验收标准

- 本地 Docker 构建成功。
- 本地 API 启动成功。
- healthcheck 可用。
- 配置文档完整。
- 默认离线。
