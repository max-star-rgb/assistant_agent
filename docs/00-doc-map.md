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

## Phase 5A 文档入口

Assistant Capability Routing Baseline 相关任务优先阅读：

- `docs/41-phase5a-assistant-capability-routing-roadmap.md`
- `docs/42-assistant-capability-routing-baseline.md`
- `docs/44-assistant-routing-eval-plan.md`
- `docs/47-phase5a-assistant-routing-review.md`

## Phase 5B 文档入口

Text-first Capabilities 相关任务优先阅读：

- `docs/48-phase5b-text-first-capabilities-roadmap.md`
- `docs/43-direct-chat-and-text-only-capabilities.md`
- `docs/49-direct-chat-provider-design.md`
- `docs/50-image-generation-provider-design.md`
- `docs/51-prompt-and-output-contracts.md`
- `docs/52-text-capability-smoke-and-safety.md`
- `docs/53-phase5b-release-checklist.md`
- `docs/54-phase5b-text-first-capabilities-review.md`
- `tasks/README_PHASE5B.md`

## Phase 5C 文档入口

Product Search / Price Compare Provider Baseline 相关任务优先阅读：

- `docs/55-phase5c-product-search-price-compare-roadmap.md`
- `docs/56-product-search-provider-design.md`
- `docs/57-price-compare-provider-design.md`
- `docs/58-product-result-and-ranking-contracts.md`
- `docs/59-product-search-smoke-and-safety.md`
- `docs/60-phase5c-release-checklist.md`
- `docs/61-phase5c-product-search-price-compare-review.md`
- `tasks/README_PHASE5C.md`

## Phase 5D 文档入口

Render / 3D 渲染能力基线相关任务优先阅读：

- `docs/62-phase5d-render-capability-roadmap.md`
- `docs/63-render-capability-contract.md`
- `docs/64-render-input-and-multistep-design.md`
- `docs/65-render-smoke-eval-api-plan.md`
- `docs/66-phase5d-render-review-checklist.md`
- `docs/67-phase5d-render-capability-review.md`
- `tasks/README_PHASE5D.md`

## Phase 5E 文档入口

End-to-End Demo Flow & Response Quality 相关任务优先阅读：

- `docs/68-phase5e-e2e-demo-flow-roadmap.md`
- `docs/69-demo-scenario-matrix.md`
- `docs/70-capability-output-contract-unification.md`
- `docs/71-response-composer-quality.md`
- `docs/72-eval-suite-layering.md`
- `docs/73-e2e-demo-runner.md`
- `docs/75-phase5e-e2e-demo-flow-review.md`
- `tasks/README_PHASE5E.md`

## Phase 5F 文档入口

Hybrid Intent Router & Planner Quality 相关任务优先阅读：

- `docs/76-phase5f-hybrid-intent-router-roadmap.md`
- `docs/77-intent-decision-schema-and-validator.md`
- `docs/78-rule-router-confidence-refactor.md`
- `docs/79-llm-intent-router-adapter.md`
- `docs/80-planner-quality-and-slot-filling.md`
- `docs/81-intent-router-eval-comparison.md`
- `docs/83-phase5f-hybrid-intent-router-review.md`
- `tasks/README_PHASE5F.md`

## Phase 5G 文档入口

Video Understanding as External MLLM Capability 相关任务优先阅读：

- `docs/84-phase5g-video-understanding-roadmap.md`
- `docs/85-video-understanding-contract.md`
- `docs/86-video-provider-adapter-and-safety.md`
- `docs/87-video-multistep-integration.md`
- `docs/88-video-smoke-eval-api-plan.md`
- `docs/89-phase5g-video-understanding-review-checklist.md`
- `docs/90-phase5g-video-understanding-review.md`
- `tasks/README_PHASE5G.md`

## Phase 5H 文档入口

Provider Safety / Retry / Cost / Trace Query 相关任务优先阅读：

- `docs/91-phase5h-provider-safety-roadmap.md`
- `docs/92-provider-error-taxonomy-and-safety-policy.md`
- `docs/93-retry-fallback-timeout-policy.md`
- `docs/94-provider-call-budget-and-cost-guard.md`
- `docs/95-trace-query-and-redaction.md`
- `docs/96-provider-safety-eval-api-plan.md`
- `docs/97-phase5h-provider-safety-review-checklist.md`
- `docs/98-phase5h-provider-safety-review.md`
- `tasks/README_PHASE5H.md`

## Phase 5I 文档入口

Memory Hardening 相关任务优先阅读：

- `docs/99-phase5i-memory-hardening-roadmap.md`
- `docs/100-memory-data-model-and-store-boundary.md`
- `docs/101-memory-retrieval-ranking-context.md`
- `docs/102-memory-write-policy-and-lifecycle.md`
- `docs/103-memory-privacy-user-isolation.md`
- `docs/104-memory-eval-api-demo-plan.md`
- `docs/105-phase5i-memory-hardening-review-checklist.md`
- `docs/106-phase5i-memory-hardening-review.md`
- `tasks/README_PHASE5I.md`

## Phase 5J 文档入口

MCP / Skills Packaging 相关任务优先阅读：

- `docs/107-phase5j-mcp-skills-packaging-roadmap.md`
- `docs/108-mcp-tool-boundary-contract-inventory.md`
- `docs/109-mcp-server-skeleton.md`
- `docs/110-skills-packaging-structure.md`
- `docs/111-skill-runbooks-and-demo-flow-packaging.md`
- `docs/112-mcp-skills-safety-eval-plan.md`
- `docs/113-phase5j-mcp-skills-review-checklist.md`
- `docs/114-phase5j-mcp-skills-review.md`
- `tasks/README_PHASE5J.md`
