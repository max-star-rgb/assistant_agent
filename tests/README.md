# Test Layers

The full pytest suite is retained as the broad offline regression check. For small scoped development, prefer a fast layer plus the tests that directly cover the changed module.

Common commands:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/unit tests/contracts -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_tool_executor.py -q
```

Marker intent:

- `fast`: unit and contract checks suitable for small changes.
- `unit`: isolated model, helper, or local service behavior.
- `contract`: adapter/tool contract behavior.
- `api`: HTTP, WebSocket, CLI, or entry-layer behavior.
- `runtime`: assistant loop, graph runtime, gateway, realtime, or routing behavior.
- `eval`: offline evaluation cases.
- `smoke`: smoke scripts and operator-facing smoke checks.
- `slow`: broader regression checks outside the small-change fast path.
- `integration`: opt-in checks that may require explicit environment setup.
- `e2e`: end-to-end demo or multi-layer workflow checks.
- `regression`: historical or phase-level behavior guards.

Default guidance:

- Small scoped changes: run `-m fast` and the relevant module test file.
- Runtime/tool/API changes: add the matching `runtime`, `api`, or domain-specific test files.
- Before merging broader changes: run the full offline pytest suite.
- Real provider tests remain opt-in through `RUN_INTEGRATION_TESTS=1` and explicit provider configuration.

Test governance:

- Repository-wide auditing, deduplication, layering, marker governance, and cleanup use
  `.codex/skills/assistant-agent-test-governance` only when the user explicitly requests them.
- Classify candidates as keep, merge, reclassify, or delete from behavioral evidence. Test
  count, coverage percentage, age, or runtime alone never authorizes deletion.
- After changing tests, keep markers, shared fixtures/builders, and this layer guide aligned;
  preserve focused failure diagnostics instead of creating oversized merged tests.
