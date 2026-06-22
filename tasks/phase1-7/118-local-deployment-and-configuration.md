# Task 118 Local Deployment and Configuration

## Goal

提供本地部署和配置文件。

## Read first

- `docs/119-phase6d-deployment-config-observability-roadmap.md`

## Requirements

- 完善 `.env.example`。
- 新增或完善 `Dockerfile`。
- 新增或完善 `docker-compose.yml`。
- 新增 `docs/configuration.md`。
- 新增 `docs/deployment-local.md`。
- 默认 mock/local。
- 不写真实 key。

## Acceptance

```bash
python -m pytest
```

如 Docker 可用，可运行构建检查；若不可用，记录原因。
