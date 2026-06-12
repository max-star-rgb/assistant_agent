# 00 文档地图：Codex 分层阅读入口

本文件告诉 Codex 如何逐层阅读项目文档。不要把所有文档一次性读完。

## 第一层：自动加载

- `AGENTS.md`：仓库级规则，Codex 会自动读取。

## 第二层：导航文档

- `docs/00-doc-map.md`：本文档，说明读哪些文件。
- `tasks/README.md`：任务顺序和阶段边界。

## 第三层：架构文档

实现跨模块能力时才读取：

- `docs/01-architecture.md`：系统总体架构。
- `docs/02-repository-layout.md`：代码目录和模块职责。
- `docs/03-agent-state.md`：AgentState、任务状态、工具调用状态。

## 第四层：模块文档

按当前任务读取：

- `docs/04-intent-and-routing.md`：意图识别、Tool Router。
- `docs/05-tool-contracts.md`：工具接口、输入输出、错误处理。
- `docs/06-memory-design.md`：短期记忆、长期记忆、视频记忆、偏好记忆。
- `docs/07-service-api.md`：FastAPI / WebSocket 接口。
- `docs/08-testing.md`：测试策略和验收标准。
- `docs/09-security-observability-cost.md`：安全、日志、成本控制。
- `docs/10-codex-usage.md`：如何用 Codex 按任务推进。

## 第五层：任务文档

每次只执行一个任务：

- `tasks/000-project-scaffold.md`
- `tasks/001-domain-schemas.md`
- `tasks/002-agent-state.md`
- `tasks/003-intent-router.md`
- `tasks/004-tool-registry.md`
- `tasks/005-memory-service.md`
- `tasks/006-vision-understanding-adapter.md`
- `tasks/007-product-search-compare.md`
- `tasks/008-image-generation-adapter.md`
- `tasks/009-render-adapter.md`
- `tasks/010-fastapi-gateway.md`
- `tasks/011-websocket-events.md`
- `tasks/012-observability-tool-history.md`
- `tasks/013-e2e-demo-flow.md`

## Codex 阅读策略

当用户说“执行 tasks/003-intent-router.md”时，只需要阅读：

1. `AGENTS.md`
2. `docs/00-doc-map.md`
3. `tasks/003-intent-router.md`
4. 任务文件中的 `Read first` 列出的文档

不要阅读无关任务，不要提前实现未来功能。
