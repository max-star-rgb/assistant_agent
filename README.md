# 多模态自主工具调用 Agent

本项目用于构建一个多模态自主工具调用 Agent。Agent 负责理解文本、图片、视频、语音等输入，识别用户真实意图，并编排视觉理解、商品搜索、比价、图片生成、3D 渲染、记忆检索等能力。

当前已完成 Phase 5J MCP / Skills Packaging。默认仍使用本地 Mock/Local 能力：

- Pydantic 领域 schema 和 AgentState。
- 规则版意图识别与 Tool Router。
- LangGraph `AgentGraphRuntime` 作为默认运行时，支持多步任务 loop。
- Tool Registry 与稳定 mock tools，真实视觉 Provider 可选接入。
- 本地记忆服务、Memory 检索策略、run history、tool call history。
- WebSocket runtime event sink、本地 TaskQueue 抽象、Graph Execution Trace。
- Failure Recovery Policy 和统一 API 错误结构。
- FastAPI `/health`、`/agent/run` 和 WebSocket `/ws/agent/{session_id}`。
- 端到端 Demo：识别视频里的鞋子、搜索相似商品、比价、生成日系海报、保存记忆。

## 本地测试

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
```

单独运行端到端 Demo 验收：

```bash
pytest tests/e2e/test_demo_flow.py
```

## Demo 请求

```json
{
  "user_id": "u1",
  "session_id": "s1",
  "text": "帮我找视频里的鞋子，比较价格，然后生成一张日系海报。",
  "video_ids": ["video_demo_1"]
}
```

该请求会使用 mock 能力完成：视频理解、商品搜索、价格比较、图片生成和记忆保存。

## Phase 4 状态

- 默认 Provider：Mock/Local。
- 可选真实 Provider：视觉理解 OpenAI/Qwen HTTP adapter。
- Integration tests：默认 skip，设置 `RUN_INTEGRATION_TESTS=1` 且配置完整后运行。
- API 协议：HTTP response 和 WebSocket 错误事件使用 `protocol_version: "v1"` 与统一 `code/message/detail/recoverable` 错误结构。
- 架构审计：见 `docs/35-phase4-architecture-review.md`。

## Phase 4.5 Smoke

真实 Vision Provider smoke 需要用户手动设置环境变量并显式运行脚本。默认 pytest 仍离线，不调用真实 Provider。

- 环境变量模板：`.env.example`
- 本地低风险样例目录：`demo_data/`
- 运行说明：`docs/39-real-provider-smoke-runbook.md`
- 成功判定：`docs/48-real-vision-smoke-success-runbook.md`

## Phase 5A 状态

Phase 5A 已完成 Assistant Capability Routing Baseline，而不是 Vision Provider hardening。Agent 会根据用户意图选择 `direct_chat`、`image_generation`、`image_understanding`、`video_understanding`、`product_search`、`price_compare`、`render_3d`、`memory_retrieval` 或 `multi_step_orchestration`。

真实 Qwen Vision smoke 已跑通，但它只作为 image/video understanding capability 的 Provider validation。真实 Provider 只能由用户手动运行 smoke 脚本触发；默认运行和默认测试仍使用 MockAdapter。

- Phase 5A 路线：`docs/41-phase5a-assistant-capability-routing-roadmap.md`
- Routing baseline：`docs/42-assistant-capability-routing-baseline.md`
- Vision validation note：`docs/45-vision-provider-validation-note.md`
- Phase 5A 审计：`docs/47-phase5a-assistant-routing-review.md`

## Phase 5B 状态

Phase 5B 已完成 Text-first Capabilities 阶段，只聚焦两个纯文本能力：

- `direct_chat`
- `image_generation`

这两个能力必须支持纯文本输入，不依赖图片或视频。Phase 5B 不接入商品搜索、比价、3D 渲染或新的 Vision hardening；默认运行和默认测试继续使用 MockAdapter，不调用真实外部 Provider。

- Phase 5B 路线：`docs/48-phase5b-text-first-capabilities-roadmap.md`
- Phase 5B 任务：`tasks/README_PHASE5B.md`
- Direct Chat Provider：`docs/49-direct-chat-provider-design.md`
- Image Generation Provider：`docs/50-image-generation-provider-design.md`
- Prompt/output contract：`docs/51-prompt-and-output-contracts.md`
- Smoke 安全说明：`docs/52-text-capability-smoke-and-safety.md`
- Phase 5B 审计：`docs/54-phase5b-text-first-capabilities-review.md`

## Phase 5C 状态

Phase 5C 已完成 Product Search / Price Compare Provider Baseline 阶段，只聚焦两个业务能力：

- `product_search`
- `price_compare`

Phase 5C 不升级意图识别，不接入 3D render，不做 Vision hardening，不进入 Harness Engineering。默认运行、默认 pytest 和默认 eval 必须离线，使用 MockAdapter 或 LocalJsonAdapter，不联网、不调用真实商品/价格 Provider。

Phase 5C 明确禁止爬虫、登录、cookie、购买、下单和支付。真实 Provider 只能由用户显式配置环境变量，并手动运行 smoke 脚本或启用 env-gated integration tests。

- Phase 5C 路线：`docs/55-phase5c-product-search-price-compare-roadmap.md`
- Phase 5C 任务：`tasks/README_PHASE5C.md`
- Product Search Provider：`docs/56-product-search-provider-design.md`
- Price Compare Provider：`docs/57-price-compare-provider-design.md`
- Product/Price contract：`docs/58-product-result-and-ranking-contracts.md`
- Smoke 安全说明：`docs/59-product-search-smoke-and-safety.md`
- Phase 5C Release Checklist：`docs/60-phase5c-release-checklist.md`
- Phase 5C 审计：`docs/61-phase5c-product-search-price-compare-review.md`

## Phase 5D 状态

Phase 5D 已完成 Render / 3D 渲染能力基线阶段，只聚焦一个轻量 Agent capability：

- `render_3d`

Phase 5D 不把项目扩展成独立 3D 渲染平台，不接入真实 Blender / Unity / Three.js，不做复杂材质系统、模型资产管理平台、渲染农场或生产级任务队列。默认运行、默认 pytest 和默认 eval 必须离线，使用 `MockRenderAdapter`，不调用真实外部 Render Provider。

真实 Render Provider 只能由用户显式配置环境变量，并手动运行 smoke 脚本或启用 env-gated integration tests。

- Phase 5D 路线：`docs/62-phase5d-render-capability-roadmap.md`
- Phase 5D 任务：`tasks/README_PHASE5D.md`
- Render contract：`docs/63-render-capability-contract.md`
- Render 多步设计：`docs/64-render-input-and-multistep-design.md`
- Render smoke/eval/API：`docs/65-render-smoke-eval-api-plan.md`
- Phase 5D 审计：`docs/67-phase5d-render-capability-review.md`

## Phase 5E 状态

Phase 5E 已完成 End-to-End Demo Flow & Response Quality 阶段。该阶段不新增真实 Provider，不接入真实外部 API，不做 MCP / Skills，不升级复杂 LLM Intent Router。

Phase 5E 只聚焦：

- demo scenario matrix
- capability output contract unification
- template-based response composer quality
- eval suite layering
- offline E2E demo runner
- Phase 5E review

Phase 5E 的目标是把 Phase 5A-5D 已完成的 capability baseline 串成可演示、可评估、可复现的端到端用户场景。默认运行、默认 pytest 和默认 eval 继续使用 MockAdapter / LocalJsonAdapter，不调用真实 Provider。

- Phase 5E 路线：`docs/68-phase5e-e2e-demo-flow-roadmap.md`
- Phase 5E 任务：`tasks/README_PHASE5E.md`
- Demo scenario matrix：`demo_data/scenarios/e2e_demo_scenarios.json`
- E2E demo runner：`scripts/run_demo_flows.py`
- Phase 5E 审计：`docs/75-phase5e-e2e-demo-flow-review.md`

## Phase 5F 状态

Phase 5F 已完成 Hybrid Intent Router & Planner Quality 阶段。该阶段不新增真实 Provider，不接入真实 API，不做 MCP / Skills。

Phase 5F 只聚焦：

- `IntentDecision` schema
- `CapabilityValidator`
- Rule Router confidence
- LLM Intent Router Adapter skeleton
- `MockLLMIntentRouter`
- Planner slot filling
- Router eval comparison
- Phase 5F review

默认 router 必须仍为 rule。LLM Router 只能 optional、mockable、default-off；默认 pytest、默认 eval 和默认 demo runner 不得调用真实 LLM 或真实 Provider。LLM 输出不能直接执行工具，必须经过 `IntentDecision` schema 校验和 `CapabilityValidator` 后，再交给 planner / LangGraph 执行。

- Phase 5F 路线：`docs/76-phase5f-hybrid-intent-router-roadmap.md`
- Phase 5F 任务：`tasks/README_PHASE5F.md`
- Phase 5F 审计：`docs/83-phase5f-hybrid-intent-router-review.md`

## Phase 5G 状态

Phase 5G 已完成 Video Understanding as External MLLM Capability 阶段，只聚焦轻量 `video_understanding` capability baseline，不做视频模型工程。

Agent 只负责识别用户是否需要视频理解、校验 video 输入、调用 `VideoUnderstandingTool`，并把 `VideoUnderstandingResult` 交给后续能力。真实的视频总结、物体识别、场景识别、动作/事件识别、OCR、商品识别等能力由外部 Video MLLM / VLM Provider 负责。

默认运行、默认 pytest、默认 eval 和默认 demo runner 必须继续使用 `MockVideoUnderstandingAdapter`，不得调用真实 Video Provider。Phase 5G 不自研视频模型，不实现复杂抽帧系统，不做实时 WebRTC，不建设视频数据库或视频监控平台。

- Phase 5G 路线：`docs/84-phase5g-video-understanding-roadmap.md`
- Phase 5G 任务：`tasks/README_PHASE5G.md`
- Phase 5G 审计：`docs/90-phase5g-video-understanding-review.md`

## Phase 5H 状态

Phase 5H 已完成 Provider Safety / Retry / Cost / Trace Query 阶段。该阶段是横向 Provider safety 层，不新增 Agent capability，不新增真实 Provider，不默认调用真实外部 Provider，不做 MCP / Skills，也不做 Memory Hardening。

Phase 5H 只聚焦：

- ProviderError taxonomy
- ProviderSafetyPolicy
- Retry / Fallback / Timeout policy
- ProviderCallBudget
- Trace query
- Redaction
- Provider safety eval / API coverage
- Phase 5H review

默认运行、默认 pytest、默认 eval 和默认 demo runner 必须继续使用 MockAdapter / LocalJsonAdapter。所有错误、trace、日志和 API 输出不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response 或敏感文件路径。

- Phase 5H 路线：`docs/91-phase5h-provider-safety-roadmap.md`
- Phase 5H 任务：`tasks/README_PHASE5H.md`
- Phase 5H 审计：`docs/98-phase5h-provider-safety-review.md`

## Phase 5I 状态

Phase 5I 已完成 Memory Hardening 阶段。该阶段不新增真实 Provider，不接外部 memory service，不接 Vector DB，不做复杂 RAG 平台，不做 MCP / Skills。

Phase 5I 只聚焦：

- MemoryItem / MemoryQuery / MemorySearchResult
- MemoryStore boundary
- Retrieval ranking
- Memory context builder
- Memory write policy
- Lifecycle / delete
- Privacy / user isolation
- Memory eval / API / demo coverage
- Phase 5I review

默认运行、默认 pytest、默认 eval 和默认 demo runner 必须继续使用 InMemoryStore / JsonlMemoryStore / MockAdapter / LocalJsonAdapter。Memory、trace、日志和 API 输出不得包含 API Key、Authorization header、Bearer token、完整 base64、完整 provider raw response、原始媒体或敏感文件路径。

- Phase 5I 路线：`docs/99-phase5i-memory-hardening-roadmap.md`
- Phase 5I 任务：`tasks/README_PHASE5I.md`
- Phase 5I 审计：`docs/106-phase5i-memory-hardening-review.md`

## Phase 5J 状态

Phase 5J 已完成 MCP / Skills Packaging 阶段。该阶段不新增业务能力，不新增真实 Provider，不默认调用真实外部 Provider，不发布远程 MCP 服务，不实现复杂 OAuth / 权限系统。

Phase 5J 只聚焦：

- MCP tool boundary
- MCP tool contract inventory
- MCP server skeleton
- offline MCP smoke
- Skills packaging structure
- skill runbooks
- MCP / Skills safety validation
- Phase 5J review

默认运行、默认 pytest、默认 eval、默认 demo runner、默认 MCP smoke 和默认 skills validation 必须继续使用 MockAdapter / LocalJsonAdapter / InMemoryStore / JsonlMemoryStore，离线运行。

- Phase 5J 路线：`docs/107-phase5j-mcp-skills-packaging-roadmap.md`
- Phase 5J 任务：`tasks/README_PHASE5J.md`
- Phase 5J 审计：`docs/114-phase5j-mcp-skills-review.md`

## 目录

```text
repo-root/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── src/
│   └── multimodal_agent/
├── docs/
├── tasks/
└── tests/
```
