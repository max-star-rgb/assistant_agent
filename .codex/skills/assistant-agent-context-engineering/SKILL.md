---
name: assistant-agent-context-engineering
description: Project-local workflow for assistant_agent context engineering work. Use when Codex needs to design, review, debug, document, or modify assistant context, prompt or context rendering, conversation history, memory context injection, tool observation compaction, context budget behavior, or any assistant_agent context pipeline.
---

# Assistant Agent Context Engineering

Use this skill as the execution wrapper for context-engineering work in the `assistant_agent` repository. The repository documentation remains the source of truth; this skill only routes work to the right artifacts and checks.

## Start

1. Locate the project root.
   - Prefer the current working directory when it contains `AGENTS.md` and `src/assistant_agent/`.
   - If those files are absent, ask for the `assistant_agent` repository path before editing.
2. Read `AGENTS.md`.
3. Read the "new conversation handoff" section at the top of `docs/CONTEXT_ENGINEERING_STATUS.md`.
4. Read the task-relevant sections of `docs/CONTEXT_ENGINEERING_STATUS.md`.
5. Search relevant source and tests before changing behavior.
6. Treat `docs/development/**` as historical only unless the user explicitly asks for historical decisions.

## Source Map

Inspect these areas as relevant:

- `src/assistant_agent/services/`: context services, trace/session behavior and runtime context assembly.
- `src/assistant_agent/services/context/sources.py` and `soul_source.py`: default-off, owner-bound editable `SOUL.md` context and last-known-good handling.
- `src/assistant_agent/services/realtime_task_state.py`: prompt-safe realtime task/call state, revisions, reusable artifacts and side-effect records.
- `src/assistant_agent/services/realtime_video_memory.py`, `realtime_video_observer.py`, and `video_context.py`: bounded realtime video observation memory, projection and diagnostics.
- `src/assistant_agent/services/durable_tasks/` and `schemas/durable_tasks.py`: trusted worker resume snapshot and durable task execution state consumed by context.
- `src/assistant_agent/agent/`: assistant loop, message flow, prompt rendering and tool observation integration.
- `src/assistant_agent/memory/`: memory context interfaces and retrieval outputs when context uses memory.
- `src/assistant_agent/tools/`: observation shape and tool-result compaction inputs.
- `tests/`: targeted tests for context rendering, history handling, budget behavior and assistant loop flow.

## Working Rules

- Preserve the distinction between conversation history, rendered assistant context, memory context and tool observations.
- Keep memory retrieval/write policy owned by memory service, not by context-rendering code.
- Avoid reintroducing old intent/router/plan dependencies into the real LLM assistant loop.
- Keep mock/offline paths useful for tests without pretending they are real LLM behavior.
- Make context-budget behavior explicit and testable when changing truncation, compaction or ordering.
- Update the authority document when the current context pipeline or handoff changes.

## Validation

Choose the smallest validation that covers the change:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest <targeted tests>
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_context_sources.py tests/test_soul_context_source.py tests/test_assistant_context_renderer.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_task_state.py tests/test_realtime_video_memory.py tests/test_realtime_video_observer.py tests/test_video_context.py tests/test_durable_task_context.py
git diff --check -- AGENTS.md docs/CONTEXT_ENGINEERING_STATUS.md src tests .codex/skills
```

Only run broader evals or demos when the change affects shared runtime behavior.
