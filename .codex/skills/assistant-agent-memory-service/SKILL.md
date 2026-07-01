---
name: assistant-agent-memory-service
description: Project-local workflow for assistant_agent memory service work. Use when Codex needs to design, review, debug, document, or modify MemoryManager, Memory Kernel, memory store or retrieval, write policy, user profile memory, memory tools, memory API, RequestIdentity, token-aware memory context, retention, export, audit, memory eval, or long-term memory boundaries.
---

# Assistant Agent Memory Service

Use this skill as the execution wrapper for memory-service work in the `assistant_agent` repository. The repository documentation remains the source of truth; this skill only routes work to the right artifacts and checks.

## Start

1. Locate the project root.
   - Prefer the current working directory when it contains `AGENTS.md` and `src/assistant_agent/`.
   - If those files are absent, ask for the `assistant_agent` repository path before editing.
2. Read `AGENTS.md`.
3. Read `docs/memory-service-architecture.md` completely enough for the task.
4. If the task explicitly concerns Memory Kernel engineering history or staged rollout, read `docs/development/memory-kernel-hardening-plan.md` as reference only.
5. Search relevant source and tests before changing behavior.
6. Treat other `docs/development/**` files as historical only unless the user explicitly asks for historical decisions.

## Source Map

Inspect these areas as relevant:

- `src/assistant_agent/memory/`: memory manager, store, retrieval, write policy and profile behavior.
- `src/assistant_agent/tools/memory_tool.py`: thin tool adapter for agent-facing memory calls.
- `src/assistant_agent/services/`: memory-related runtime services, identity and context integration.
- `src/assistant_agent/agent/`: assistant loop decisions around memory tool calls and memory context.
- `tests/`: targeted tests for memory boundary, retrieval eval, tool adapter and identity behavior.

## Working Rules

- Keep memory tools thin: bind `ToolContext`, adapt inputs, call `MemoryManager`, and wrap `ToolResult`.
- Keep retrieval ranking, write policy, profile merge, TTL, audit and direct store access inside memory service layers.
- Preserve LLM-first memory tool selection; do not replace it with keyword or vector override routing.
- Keep all writes behind `MemoryWritePolicy` and explicit `source_intent` where required.
- Do not store secrets, raw provider responses or real user data in tracked files.
- Update the authority document when memory boundaries, APIs or routing rules change.

## Validation

Choose the smallest validation that covers the change:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest <targeted tests>
git diff --check -- AGENTS.md docs/memory-service-architecture.md src tests .codex/skills
```

Only run broader evals or demos when the change affects shared runtime behavior.
