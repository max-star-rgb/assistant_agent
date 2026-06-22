---
name: phase8-runner
description: Execute Phase 8 ReAct assistant architecture tasks for assistant_agent, including A1 assistant loop, A2 planning, and A3 reflection.
version: 2.0.0
---

# Skill: phase8-runner

## Purpose

Use this skill to execute Phase 8 tasks in the repository.

Phase 8 upgrades the agent architecture from an intent-router workflow to a ReAct-style assistant architecture.

This skill can execute all Phase 8 tasks in one run when explicitly requested.

## Phase 8 task set

Stable task IDs:

```text
phase8_A1_react_assistant_loop
phase8_A2_planning_extension
phase8_A3_reflection_extension
```

## Execution modes

### A1-only mode

Use when the user asks for only the current assistant brain architecture.

Execute only:

```text
phase8_A1_react_assistant_loop
```

### Full Phase 8 mode

Use when the user asks to execute Phase 8, all Phase 8 tasks, or the full assistant architecture upgrade.

Execute all tasks in this order:

```text
phase8_A1_react_assistant_loop
phase8_A2_planning_extension
phase8_A3_reflection_extension
```

Do not treat “all tasks” as parallel implementation. Execute in dependency order, but in the same work session.

## Required reading

Always read these first:

```text
README.md
docs/phase8_A1_react_assistant_loop.md
docs/phase8_A2_planning_extension.md
docs/phase8_A3_reflection_extension.md
tasks/phase8_A1_react_assistant_loop.md
tasks/phase8_A2_planning_extension.md
tasks/phase8_A3_reflection_extension.md
```

Then inspect the repository files relevant to implementation, typically:

```text
src/multimodal_agent/agent/runtime.py
src/multimodal_agent/agent/conditional_graph.py
src/multimodal_agent/agent/graph_nodes.py
src/multimodal_agent/agent/state.py
src/multimodal_agent/agent/tool_executor.py
src/multimodal_agent/tools/registry.py
src/multimodal_agent/services/chat_adapter.py
src/multimodal_agent/config.py
scripts/run_demo_flows.py
demo_data/scenarios/e2e_demo_scenarios.json
tests/
```

If paths differ, find the equivalent files.

## Global rules

Follow these rules for all Phase 8 tasks:

1. Preserve existing behavior unless the task explicitly changes it.
2. Keep the old `conditional` graph available.
3. Do not delete `conditional_graph.py`.
4. Do not delete the old `chat_node` unless a later explicit cleanup task exists.
5. Keep `AGENT_GRAPH_MODE=conditional` as default.
6. Add new graph modes as opt-in.
7. Use existing `ToolExecutor` for all tool execution.
8. Do not let `assistant_node`, `planner_node`, or `reflection_node` call provider HTTP APIs directly.
9. Do not bypass the registry/tool executor layer.
10. Tests must be mock/local/offline.
11. Do not call real external APIs in tests.
12. Do not commit API keys, tokens, secrets, bearer strings, base64 payloads, or raw provider responses.
13. Enforce loop limits.
14. Prefer adding new files over rewriting large existing files.
15. Keep code typed and consistent with the existing repository style.

## A1 execution requirements

When executing `phase8_A1_react_assistant_loop`, implement:

```text
AssistantDecision
assistant_node
execute_requested_tool_node
route_after_assistant
build_assistant_loop_graph
AGENT_GRAPH_MODE support
MAX_TOOL_ITERATIONS support
registry tool descriptions if missing
state or graph-state fields for assistant decision and observations
assistant_loop demos
offline tests
```

A1 target graph:

```text
START
  ↓
load_memory
  ↓
assistant_node
  ↓
route_after_assistant
  ├─ execute_tool → assistant_node
  └─ finish → save_memory → END
```

A1 acceptance checklist:

```text
old conditional graph still works
assistant_loop graph works
direct chat needs no tools
image generation calls image_generation
unknown tool is safe
tool failure is safe
invalid JSON is safe
max loop limit works
no external APIs in tests
no secrets leaked
```

## A2 execution requirements

When executing `phase8_A2_planning_extension`, first ensure A1 works.

Implement:

```text
planning decision or make_plan route
AgentPlan / PlanStep schema
planner_node
execute_plan_loop or equivalent reuse of existing plan nodes
step dependency resolution
outputs by step ID
bounded plan limits
planning demos
offline tests
```

A2 must preserve A1 behavior.

A2 acceptance checklist:

```text
simple tasks still use A1
complex tasks can create plan
plan uses known tools only
steps execute through ToolExecutor
step outputs are stored by step_id
dependencies resolve deterministically
required step failure is safe
plan limits work
no external APIs in tests
no secrets leaked
```

## A3 execution requirements

When executing `phase8_A3_reflection_extension`, first ensure A1 works. If A2 was implemented, ensure A2 still works.

Implement:

```text
ReflectionDecision schema
reflection_node
route_after_reflection
bounded retry policy
optional final-answer reflection if enabled by config
reflection demos
offline tests
```

A3 must preserve A1 and A2 behavior.

A3 acceptance checklist:

```text
sufficient tool result passes
empty result is detected
failed tool can retry within limit
retry limit stops retries
reflection can ask follow-up
reflection can fail safely
final answer reflection works if enabled
no infinite reflection loop
no external APIs in tests
no secrets leaked
```

## Suggested command policy

Use existing project commands. Examples:

```bash
python -m pytest
AGENT_GRAPH_MODE=assistant_loop python scripts/run_demo_flows.py --scenario assistant_loop_direct_chat
AGENT_GRAPH_MODE=assistant_loop python scripts/run_demo_flows.py --scenario assistant_loop_image_generation
ruff check
mypy
```

Only run commands that are valid for the repository.

## Reporting format

After completing the requested execution mode, report:

```text
Phase 8 execution complete.

Scope executed:
- phase8_A1_react_assistant_loop: done / skipped / failed
- phase8_A2_planning_extension: done / skipped / failed
- phase8_A3_reflection_extension: done / skipped / failed

Files added:
- ...

Files changed:
- ...

Config added:
- ...

Graphs added/changed:
- ...

Tests run:
- ...

Demo commands run:
- ...

External API calls:
- none / explain

Secret leakage check:
- passed / explain

Remaining issues:
- ...
```
