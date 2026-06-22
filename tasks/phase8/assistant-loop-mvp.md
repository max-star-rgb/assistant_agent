# Phase 8A Task：Assistant Loop MVP

## Goal

把 `chat_node` 从 intent-router 的一个可选分支升级为中心 `assistant_node`，并新增 assistant-driven tool loop graph。

## Read first

```text
AGENTS.md
docs/phase8/README.md
docs/phase8/assistant-loop-architecture-upgrade.md
docs/phase8/planning-and-reflection-roadmap.md
docs/125-phase7-production-readiness-roadmap.md
docs/126-phase7a-runtime-configuration-profiles.md
src/multimodal_agent/agent/conditional_graph.py
src/multimodal_agent/agent/graph_nodes.py
src/multimodal_agent/agent/runtime.py
src/multimodal_agent/tools/
src/multimodal_agent/adapters/
tests/
```

如果 Phase 7 文档路径不存在，请查找最接近的 runtime profile / production readiness 文档。

## Scope

新增：

```text
src/multimodal_agent/agent/assistant_loop_graph.py
src/multimodal_agent/agent/assistant_loop_nodes.py
src/multimodal_agent/schemas/assistant_decision.py
src/multimodal_agent/schemas/tool_observation.py
```

修改：

```text
runtime 图选择配置
AgentState / graph_state 必要字段
response composer 必要兼容
demo/eval scenarios
tests
.env.example / docs/configuration.md 如需要
```

## Requirements

- 保留旧 `conditional_graph.py`。
- 默认 graph mode 仍为 `conditional`。
- 新增 `MULTIMODAL_AGENT_GRAPH_MODE=assistant_loop` 后才启用新图。
- `assistant_node` 是中心大脑。
- `assistant_node` 只输出 `AssistantDecision`，不直接执行工具。
- `execute_requested_tool_node` 复用现有 `ToolExecutor`。
- 所有工具、模型、能力服务都作为 action。
- 第一版只支持 `final_answer` / `tool_call` / `ask_followup`。
- 增加 `loop_count` / `max_tool_iterations` 防止死循环。
- unknown tool、invalid tool_input、tool failure 都必须安全处理。
- local_demo / offline_eval 下必须 mock/local/offline。
- 不调用真实 Provider。
- 不写 API Key。
- 不修改 `tools/__init__.py`。
- 不实现复杂 planning。
- 不实现复杂 reflection。
- 不开始 Phase 8B 或 Phase 8C。

## Tests

至少覆盖：

- direct chat -> final_answer，不调用工具。
- image generation -> tool_call image_generation。
- image description -> image_understanding，不调用 render_3d。
- explicit 3D render -> render_3d。
- product search + price compare -> product_search -> price_compare。
- unknown tool 被拒绝。
- tool failure 能转为 ToolObservation。
- loop 超限能停止。
- offline_eval 不调用真实 Provider。
- conditional graph 旧默认行为不破坏。

## Acceptance

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
git status --short
```

## Stop condition

完成 Phase 8A 后停止，等待用户审查新图行为。

不要自动开始 Phase 8B planning。
