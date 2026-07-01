---
name: assistant-agent-collaboration
description: Project-local workflow for assistant_agent agent collaboration work. Use when Codex needs to design, review, debug, document, or modify multi-agent instances, AgentDirectory, AgentGateway, /agents/run, A2A/JSON-RPC adapters, AgentCommunicationService, delegate_to_agent, AgentTask/AgentMessage, AgentTransport, delegation policy, gateway control-plane records, pilot readiness, evidence collection, OpenClaw-like reference mapping, or cross-agent session, memory, context, budget and loop isolation.
---

# Assistant Agent Collaboration

Use this skill as the execution wrapper for agent collaboration work in the `assistant_agent` repository. It covers internal multi-agent communication, the explicit gateway, A2A adapters, control-plane gateway observability, and OpenClaw-style reference mapping.

The repository documentation and source code remain authoritative. This skill routes work to the right authority instead of duplicating architecture detail.

## Start

1. Locate the project root.
   - Prefer the current working directory when it contains `AGENTS.md` and `src/assistant_agent/`.
   - If those files are absent, ask for the `assistant_agent` repository path before editing.
2. Read `AGENTS.md`.
3. Read `docs/agent-communication-routing.md` for multi-agent, gateway, A2A, delegation, control-plane, pilot readiness or transport work.
4. Read additional authority only when the task crosses that boundary:
   - `docs/tool-calling-architecture.md` for `delegate_to_agent` tool registration, validation, execution or tool results.
   - `docs/memory-service-architecture.md` for identity, memory APIs, memory isolation or memory context across agents.
   - `docs/CONTEXT_ENGINEERING_STATUS.md` for delegated context filtering, compaction or context-budget behavior.
5. For OpenClaw comparisons, map the concept to the matching project authority above before proposing code.
6. Search relevant source and tests before changing behavior.
7. Treat `docs/development/**` as historical only unless the user explicitly asks for historical decisions, operator runbook details or OpenClaw reference links.

## Source Map

Inspect these areas as relevant:

- `src/assistant_agent/schemas/agent_communication.py`: protocol-neutral message, task, artifact, session and directory contracts.
- `src/assistant_agent/services/agent_directory.py`: agent IDs, capabilities, enablement and directory config.
- `src/assistant_agent/services/agent_communication.py`: service boundary for sending tasks through enabled transports.
- `src/assistant_agent/services/agent_gateway.py`: explicit local gateway and controller/worker factory.
- `src/assistant_agent/services/agent_routing_policy.py`: deterministic gateway route selection.
- `src/assistant_agent/services/agent_delegation_policy.py`: permission, allowed-target, depth, timeout and loop controls.
- `src/assistant_agent/services/agent_delegation_context.py`: child-safe context filtering and artifact summaries.
- `src/assistant_agent/services/agent_transports.py`: local and outbound A2A transport implementations.
- `src/assistant_agent/services/a2a_adapter.py` and `api/routes_a2a.py`: inbound agent card and JSON-RPC adapter.
- `src/assistant_agent/services/agent_control_plane.py`: redacted gateway run, route, delegation, budget, audit and replay-preview records.
- `src/assistant_agent/tools/agent_delegation_tool.py`: thin agent-callable `delegate_to_agent` tool.
- `scripts/check_pilot_readiness.py` and `scripts/collect_pilot_evidence.py`: local readiness and evidence workflows.
- `tests/test_agent_communication_routing.py`, `tests/test_agent_gateway.py`, `tests/test_agent_routing_policy.py`, `tests/test_api_a2a.py`, `tests/test_a2a_json_rpc_transport.py`, and `tests/test_agent_pilot_readiness.py`.

## Working Rules

- Keep `/agent/run` as the default single-agent path; only `/agents/run` enters the explicit gateway.
- Preserve default local/offline behavior and do not register delegation in the default registry.
- Keep internal contracts protocol-neutral; A2A/JSON-RPC is an adapter, not the core runtime model.
- Keep agent-to-agent calls behind `delegate_to_agent`, validator, executor, registry, `AgentCommunicationService`, policy and transport boundaries.
- Preserve deterministic route priority: explicit target, routing table, unique capability match, controller fallback, default agent.
- Keep inbound A2A as a protocol adapter over `AgentGateway`; do not call providers, memory stores or runtimes directly from A2A route code.
- Keep outbound A2A default-disabled, explicitly configured and allowlisted.
- Enforce user/session isolation, delegation depth, repeated-pair and ping-pong controls.
- Do not forward parent raw memory context, parent full conversation history, hidden reasoning, raw provider responses or inline media bodies to child agents.
- Redact control-plane output: no secrets, raw provider payloads, hidden reasoning, raw memory content, inline media bodies or parent conversation history.
- Treat OpenClaw as a reference model, not a compatibility target or project authority.
- When OpenClaw and project docs differ, follow project docs and explain the difference.

## Validation

Choose the smallest validation that covers the change:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_communication_routing.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_agent_gateway.py tests/test_agent_routing_policy.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_api_a2a.py tests/test_a2a_json_rpc_transport.py tests/test_agent_pilot_readiness.py
git diff --check -- AGENTS.md docs/agent-communication-routing.md src tests scripts .codex/skills
```

Run only the subsets relevant to the files changed; for documentation or skill-only changes, `quick_validate.py` and `git diff --check` are sufficient.
