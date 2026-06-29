# Development

## Environment

Use the project Python environment with test dependencies installed.

```bash
python scripts/check_env.py
python -m pytest
```

If your shell `python` points at a base environment without pytest, run the project env Python directly.

## Common Commands

```bash
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
```

Release checklist:

```text
docs/release-checklist.md
```

## Coding Guidelines

- Keep business imports pointed at concrete modules.
- Use `apply_patch` for manual edits.
- Keep default runtime mock/local/offline.
- Add tests for behavior changes.
- Do not commit secrets, real media, generated assets, rendered artifacts, logs, or raw Provider responses.

## Task Docs

Current user-facing and developer-facing docs are the consolidated guides linked from README. Historical `tasks/`, `prompts/`, and repository-local `skills/` materials were removed after user confirmation; remaining phase/background material lives under `docs/archive/` only when it still has reference value.

## Architecture And Boundary Docs

- `docs/CONTEXT_ENGINEERING_STATUS.md`: current assistant context, conversation compaction, context budget, tool observation compaction, session summary, trace/debug context status, and new-dialogue quick handoff.
- `docs/context-engineering-walkthrough.md`: owner-facing walkthrough for assistant context data flow, boundaries, compaction, and debugging; read when explanation is needed, not as the default engineering entry.
- `docs/memory-service-architecture.md`: current memory service ownership, retrieval, write policy, profile memory, audit, and boundary rules.
- `docs/architecture-layers.md`: stable layer ownership and governance boundary checklist.

## Development Plans

- `docs/development/agent-control-plane-plan.md`: phased Local Multi-Agent Gateway + inbound A2A control-plane plan. Use it for gateway, routing, delegation safety, A2A conformance, outbound A2A pilot, and pilot-readiness work after reading `docs/agent-communication-routing.md`.
- `docs/development/memory-kernel-hardening-plan.md`: phased Memory Kernel hardening plan. Use it for future memory engineering work after reading `docs/memory-service-architecture.md`.
- `docs/development/context-engine-memory-policy-plan.md`: completed staged Context Engine + Memory Policy implementation log. Use it only for historical decisions or phase traceability after reading `docs/CONTEXT_ENGINEERING_STATUS.md`.
