---
name: assistant-agent-tool-calling
description: Project-local workflow for assistant_agent tool calling work. Use when Codex needs to design, review, debug, document, or modify ToolSpec, ActionValidator, ToolExecutor, ToolRegistry, provider-native tool calls, MCP tool_run, tool observations, tool retry/recovery, tool budgets, or any assistant_agent tool execution chain.
---

# Assistant Agent Tool Calling

Use this skill as the execution wrapper for tool-calling work in the `assistant_agent` repository. The repository documentation remains the source of truth; do not copy architecture detail into this skill.

## Start

1. Locate the project root.
   - Prefer the current working directory when it contains `AGENTS.md` and `src/assistant_agent/`.
   - If those files are absent, ask for the `assistant_agent` repository path before editing.
2. Read `AGENTS.md`.
3. Read `docs/tool-calling-architecture.md` completely enough for the task.
4. Search relevant source and tests before changing behavior.
5. Treat `docs/development/**` as historical only unless the user explicitly asks for historical decisions.

## Source Map

Inspect these areas as relevant:

- `src/assistant_agent/agent/`: assistant loop, decision handling, validation and execution flow.
- `src/assistant_agent/tools/`: registry, tool implementations, policy and audit behavior.
- `src/assistant_agent/services/`: runtime services, MCP or tool-adjacent orchestration.
- `src/assistant_agent/providers/`: provider-native tool call adapters and mock/real boundaries.
- `tests/`: targeted tests for validators, executor, assistant loop, registry and provider integration.

## Working Rules

- Keep tool calls behind validator, executor, registry, policy and audit boundaries.
- Return structured `ToolResult` or documented observation objects; do not replace them with loose strings.
- Preserve mock/local/offline defaults for tests and demos.
- Do not enable real external providers just because keys exist.
- Keep provider-native tool call support as an adapter path, not a bypass around tool governance.
- For new tools, add or update the tool spec, validation, execution behavior, tests and docs together.
- For failures, return explainable errors to the agent instead of leaking unhandled exceptions.

## Validation

Choose the smallest validation that covers the change:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest <targeted tests>
git diff --check -- AGENTS.md docs/tool-calling-architecture.md src tests .codex/skills
```

Only run broader evals or demos when the change affects shared runtime behavior.
