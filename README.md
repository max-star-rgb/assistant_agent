# 多模态自主工具调用 Agent

本项目用于构建一个多模态自主工具调用 Agent。Agent 负责理解文本、图片、视频、语音等输入，识别用户真实意图，并编排视觉理解、商品搜索、比价、图片生成、3D 渲染、记忆检索等能力。

当前已完成 Phase 4 生产化边界增强，默认仍使用本地 Mock/Local 能力：

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

Phase 5A 主线是 Assistant Capability Routing Baseline，而不是 Vision Provider hardening。Agent 应根据用户意图选择 `direct_chat`、`image_generation`、`image_understanding`、`video_understanding`、`product_search`、`price_compare`、`render_3d`、`memory_retrieval` 或 `multi_step_orchestration`。

真实 Qwen Vision smoke 已跑通，但它只作为 image/video understanding capability 的 Provider validation。真实 Provider 只能由用户手动运行 smoke 脚本触发；默认运行和默认测试仍使用 MockAdapter。

- Phase 5A 路线：`docs/41-phase5a-assistant-capability-routing-roadmap.md`
- Routing baseline：`docs/42-assistant-capability-routing-baseline.md`
- Vision validation note：`docs/45-vision-provider-validation-note.md`

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
