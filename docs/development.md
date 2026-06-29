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
python scripts/validate_skills.py
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

Task specs live in `tasks/`. Phase docs live in `docs/`. They are implementation history and planning material; user-facing docs are the consolidated guides linked from README.

## Development Plans

- `docs/development/agent-control-plane-plan.md`: phased Local Multi-Agent Gateway + inbound A2A control-plane plan. Use it for gateway, routing, delegation safety, A2A conformance, outbound A2A pilot, and pilot-readiness work after reading `docs/agent-communication-routing.md`.
- `docs/development/memory-kernel-hardening-plan.md`: phased Memory Kernel hardening plan. Use it for future memory engineering work after reading `docs/memory-service-architecture.md`.
- `docs/development/context-engine-memory-policy-plan.md`: step-by-step Context Engine + Memory Policy plan. Use it for future context compaction, session summary, LLM compactor, token budget, and memory promotion work.
