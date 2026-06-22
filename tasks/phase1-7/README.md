# Tasks：按阶段实现顺序

每次只执行一个 task。不要跨阶段实现。

## Phase 0：项目基础

1. `000-project-scaffold.md`：创建项目骨架、pyproject、基础测试。
2. `001-domain-schemas.md`：定义核心 Pydantic schemas。
3. `002-agent-state.md`：实现 AgentState 与状态更新工具。

## Phase 1：Agent 决策核心

4. `003-intent-router.md`：意图识别与初版路由。
5. `004-tool-registry.md`：统一 Tool Registry 和 mock tools。
6. `005-memory-service.md`：MVP 记忆存储与检索。

## Phase 2：能力适配器

7. `006-vision-understanding-adapter.md`：图片/视频理解 mock adapter。
8. `007-product-search-compare.md`：商品搜索与比价 mock adapter。
9. `008-image-generation-adapter.md`：图片生成 mock adapter。
10. `009-render-adapter.md`：3D 渲染 mock adapter。

## Phase 3：服务化

11. `010-fastapi-gateway.md`：FastAPI 对外入口。
12. `011-websocket-events.md`：长任务事件与进度推送。
13. `012-observability-tool-history.md`：工具调用日志与运行记录。

## Phase 4：端到端闭环

14. `013-e2e-demo-flow.md`：完整 Demo 流程。

## 执行规则

每个任务都包含：

- Goal
- Read first
- Scope
- Steps
- Acceptance
- Out of scope

Codex 必须按这些边界执行。
