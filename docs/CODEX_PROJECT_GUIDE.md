# Codex Project Guide

## 1. Project Purpose

This repository implements a local-first multimodal assistant agent. The current system routes user text, image refs, video refs, product requests, render requests, and memory requests through structured tools. The default runtime is mock/local/offline, so CLI, API, tests, evals, and demo flows run without API keys.

The current core is a LangGraph ReAct-style assistant loop. The assistant proposes final answers, follow-up questions, plan-mode transitions, or tool calls; local code validates and executes tool calls through the registry/executor boundary. Real chat/provider paths exist only as explicit opt-in smoke/pilot configuration, not as default behavior.

## 2. Current Architecture

Main directories:

- `src/multimodal_agent/agent/`: Agent runtime, assistant loop, compatibility conditional graph, action validation, tool execution, response composition, loop/plan guards.
- `src/multimodal_agent/tools/`: Agent-callable tool boundary and `ToolSpec` contracts.
- `src/multimodal_agent/services/`: Runtime services such as chat adapters, provider selection, trace/session/run history, context building, agent communication routing, demo examples, provider readiness, and memory audit.
- `src/multimodal_agent/providers/`: Optional real provider adapters such as Ark, Qwen, and Haodanku.
- `src/multimodal_agent/memory/`: `MemoryManager`, stores, retrieval, write policy, local JSONL/in-memory memory.
- `src/multimodal_agent/api/`: FastAPI HTTP routes, WebSocket stream, and static Web Console.
- `scripts/`: Local CLI/server/client, offline eval/demo runners, smoke scripts, validation helpers.
- `tests/`: Pytest regression suite, contract tests, e2e tests, opt-in integration tests, and eval cases.

Current data flow:

```text
UserRequest
  -> AgentGraphRuntime
  -> load_memory
  -> assistant_node
  -> AssistantDecision
  -> ActionValidator
  -> ToolExecutor
  -> ToolRegistry
  -> Tool / Adapter / MemoryManager
  -> ToolObservation
  -> assistant_node
  -> final_answer / ask_followup
  -> compose_response
  -> save_memory
  -> AgentRunResponse
```

Relationship between subsystems:

- Agent/graph owns orchestration and state transitions.
- Provider adapters implement specific model/API backends, but the assistant never calls them directly.
- Tools are the stable capability boundary between Agent decisions and adapters.
- Memory is accessed through `MemoryManager` and memory tools; API/Agent should not bypass store governance.
- Agent communication is an optional local boundary for multi-agent routing. The current default `/agent/run`, CLI, eval, and Web demo paths remain single `agent.default`; the separate `/agents/run` endpoint uses `AgentGateway` for explicit local routing. Inbound A2A-compatible discovery and JSON-RPC are exposed through `/.well-known/agent-card.json` and `/a2a/rpc`, both as protocol adapters over the local gateway. `delegate_to_agent` remains registry-level opt-in and is enabled for the gateway controller runtime only; communication services must not change default CLI/API/demo behavior.
- API and WebSocket wrap the same `AgentGraphRuntime`.
- Demo and eval scripts use the same runtime with deterministic local inputs.

Default capabilities are mock/local/offline:

- Chat: `MockChatAdapter`
- Vision/video: mock adapters unless explicit provider selectors are allowed
- Product search/price compare: mock or local JSON/local deterministic providers
- Image generation/render: mock/local output refs
- Memory: in-memory or local JSONL when configured

Real provider opt-in:

- Real/network provider selectors take effect only under `MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke` or `pilot`.
- Setting API keys alone must not enable a real provider.
- Real provider smoke scripts are manual and must not be triggered by default pytest/evals/demo/API paths.

## 3. Repository Map

| path | responsibility | edit policy |
| --- | --- | --- |
| `README.md` | Human project entry and common commands | docs only |
| `AGENTS.md` | Codex behavior rules and repository guardrails | docs only |
| `docs/` | Current and historical project documentation | safe to edit |
| `docs/CODEX_PROJECT_GUIDE.md` | Current Codex project understanding entry | safe to edit |
| `docs/DOCS_INDEX.md` | Documentation inventory and cleanup status | safe to edit |
| `docs/TESTS_REVIEW.md` | Read-only tests audit and cleanup guidance | safe to edit |
| `docs/phase1-7/` | Historical phase docs and reviews | docs only |
| `docs/phase8/` | Phase 8 architecture/task background | docs only |
| `tasks/` | Historical and active task specs | docs only |
| `prompts/` | Historical prompt starters | docs only |
| `skills/` | Repository-local Codex skills and old phase runners | docs only |
| `haodanku-openapi-docs/` | Haodanku provider reference docs | docs only |
| `src/**` | Application source code | edit only for implementation tasks |
| `tests/**` | Test suite and eval cases | update with behavior changes |
| `scripts/` | CLI/server/eval/demo/smoke helpers | read-only unless explicitly requested |
| `demo_data/` | Safe local demo fixtures | read-only unless explicitly requested |
| `.env` | Local untracked environment file | do not touch |
| `.env.example` | Placeholder-only config template | docs only |
| `.local/`, `.pytest_cache/`, `__pycache__/` | Local/generated runtime artifacts | do not touch |

## 4. Key Runtime Rules

- Default mode is local/mock/offline.
- Do not automatically call real Providers.
- Do not write API keys, tokens, bearer strings, or real secrets.
- Do not create or modify a real `.env`.
- Do not commit real media, generated images, rendered artifacts, large local files, logs, or raw provider responses.
- Do not silently fall back from real provider failure to mock success.
- All tool execution must go through `AssistantDecision -> ActionValidator -> ToolExecutor -> ToolRegistry -> Tool`.
- Keep trace/API/debug output redacted.

## 5. Common Development Tasks

| 要做什么 | 先看哪里 | 可改哪里 | 验收命令 |
| --- | --- | --- | --- |
| 修改文档 | `README.md`, `AGENTS.md`, `docs/CODEX_PROJECT_GUIDE.md`, `docs/DOCS_INDEX.md` | `docs/**`, `README.md`, `AGENTS.md` | `git diff --stat`, `python scripts/check_env.py` |
| 新增 demo 场景 | `docs/demo-flows.md`, `demo_data/scenarios/e2e_demo_scenarios.json`, `scripts/run_demo_flows.py` | `demo_data/**`, tests if explicitly requested | `python scripts/run_demo_flows.py`, `python -m pytest tests/test_demo_scenario_matrix.py` |
| 调整 provider mock | `docs/configuration.md`, `docs/provider-setup.md`, relevant service adapter, provider tests | `src/multimodal_agent/services/**`, `src/multimodal_agent/tools/**`, tests | `python -m pytest tests/test_provider_config.py tests/test_provider_selection.py` |
| 调整 memory 行为 | `docs/memory-service-architecture.md`, `docs/development/memory-kernel-hardening-plan.md` for engineering hardening, `src/multimodal_agent/memory/**`, memory tools/services, memory tests | `src/multimodal_agent/memory/**`, memory tools/services, tests | `python -m pytest tests/test_memory_manager.py tests/test_memory_*` |
| 调整 agent communication 行为 | `docs/agent-communication-routing.md`, `docs/development/agent-control-plane-plan.md`, `src/multimodal_agent/schemas/agent_communication.py`, `src/multimodal_agent/services/agent_*.py`, `src/multimodal_agent/tools/agent_delegation_tool.py` | agent communication schemas/services/tools/routes as needed, tests | `python -m pytest tests/test_agent_communication_*.py tests/test_agent_gateway.py tests/test_api_a2a.py` |
| 更新 eval | `scripts/run_evals.py`, `tests/evals/eval_cases.json`, `docs/development.md` | `tests/evals/**`, `scripts/run_evals.py` if requested | `python scripts/run_evals.py` |
| 更新 API 文档 | `docs/observability-local.md`, `src/multimodal_agent/api/routes_agent.py`, API tests | docs first; source only in implementation tasks | `python -m pytest tests/test_api_* tests/test_websocket_*` |
| Work on Haodanku provider | `haodanku-openapi-docs/AI使用说明.md`, `haodanku-openapi-docs/接口目录.md`, relevant interface doc | provider docs/source only when task asks | `python -m pytest tests/test_haodanku_product_search_adapter.py` |

## 6. Testing and Validation

Use the project Python environment when available:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_demo_flows.py
```

Additional commands when configured/available:

```bash
ruff check
ruff format --check
mypy
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
```

Current validation snapshot on 2026-06-26:

- `scripts/check_env.py`: passed.
- `python -m pytest -p no:cacheprovider`: 961 passed, 8 skipped.
- `scripts/run_evals.py`: 99 passed, 0 failed.
- `scripts/run_demo_flows.py`: 20 passed, 0 failed.
- `scripts/smoke_mcp_tools.py`: passed, `ok=true`.
- `scripts/validate_skills.py`: passed, `ok=true`, 18 skills.
- `python -m ruff check .`: unavailable, `No module named ruff`.
- `python -m ruff format --check .`: unavailable, `No module named ruff`.
- `python -m mypy .`: unavailable, `No module named mypy`.

If a command is missing or fails during a docs-only task, record the command and failure reason. Do not modify `src/**` just to make validation pass in this task.

## 7. Documentation Policy

- `README.md` is the human entry point.
- `AGENTS.md` is the agent behavior and repository guardrail entry.
- `docs/CODEX_PROJECT_GUIDE.md` is the current Codex project understanding entry.
- `docs/DOCS_INDEX.md` is the documentation inventory and cleanup status source.
- `docs/memory-service-architecture.md` is the current memory service architecture and routing entry.
- `docs/development/memory-kernel-hardening-plan.md` is the phased memory engineering hardening plan.
- `docs/development/agent-control-plane-plan.md` is the phased local multi-agent gateway and A2A control-plane development plan.
- `docs/agent-communication-routing.md` is the current agent communication routing and A2A adapter boundary entry.
- `docs/TESTS_REVIEW.md` is the tests cleanup/readiness audit.
- Top-level `docs/*.md` are current user/developer references unless the index says otherwise.
- Historical phase/task/skill docs are retained or archived by default, not deleted directly.
- A document must first be marked `delete-candidate` in `docs/DOCS_INDEX.md` and pass human review before deletion.
- Do not treat "old" as "delete." Old docs can still be useful history or design rationale.

## 8. Known Open Questions

- Whether old `conditional` graph and intent-router/planner compatibility tests remain product requirements or can later be archived.
- Whether `prompts/**` should remain tracked after Phase 8, or be moved to a historical archive.
- Whether `skills/phase8-runner/SKILL.md` should be rewritten to match current paths/defaults or archived.
- Whether `hello_agent_latest.docx` should stay in the repo, be exported to Markdown, or be archived outside the normal doc tree.
- Whether tracked generated/temp-like files such as `prompts/phase8/.~lock.run-assistant-loop-mvp.md#` can be deleted after human review.
- Whether `tests/__pycache__` and other generated cache directories are intentionally tracked or should be cleaned in a separate non-code task.
