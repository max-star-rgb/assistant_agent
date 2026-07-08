# Phase 3 Skill System v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden repo-local skills into Skill System v1 without adding a second execution runtime.

**Architecture:** Phase 3 builds on the existing repo-local `skills/<skill_id>/SKILL.md` capability loader. A skill remains prompt-safe capability metadata backed by governed tools; it never executes directly, never creates `run_skill`, and never bypasses `ActionValidator -> ToolExecutor -> ToolRegistry`. V1 adds explicit permissions, local registry/audit gate coverage, enable/disable behavior, and roadmap/docs gates while avoiding marketplace, workflow engine, user-uploaded skills, or memory schema ownership.

**Tech Stack:** Python, existing context skill loader, capability catalog, `ToolRegistry`, `ToolSpec`, pytest, docs.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Do not add dependencies.
- Do not introduce a skill execution engine, `run_skill` tool, marketplace, user upload flow, workflow engine, memory schema, or independent eval system.
- Skills may only describe when to use existing governed tools.
- Tool execution must still go through `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Disabled/manual-only/invalid skills must be omitted from prompt context and recorded as prompt-safe issues.

---

## Task 1: Add Skill Manifest Permissions

**Status:** Planned.

**Files:**
- Modify: `src/assistant_agent/services/context/skill_loader.py`
- Modify: `src/assistant_agent/schemas/context.py`
- Modify: `src/assistant_agent/services/context/capability_catalog.py`
- Modify: `skills/realtime_web_search/SKILL.md`
- Modify: `tests/test_skill_loader.py`
- Modify: `tests/test_tool_catalog.py`
- Modify: `tests/test_assistant_context_renderer.py`

**Acceptance:**
- `SKILL.md` supports a `## Permissions` section.
- Each governed tool must have a matching `tool:<tool_name>` permission.
- Missing tool permission omits the descriptor and records `missing_tool_permission`.
- Prompt-rendered capability descriptors include permissions.
- Existing repo-local `realtime_web_search` declares `tool:web_search`.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_skill_loader.py tests/test_tool_catalog.py tests/test_assistant_context_renderer.py -q
git diff --check -- src/assistant_agent/services/context/skill_loader.py src/assistant_agent/schemas/context.py src/assistant_agent/services/context/capability_catalog.py skills/realtime_web_search/SKILL.md tests/test_skill_loader.py tests/test_tool_catalog.py tests/test_assistant_context_renderer.py
```

## Task 2: Add Phase 3 Skill Governance Gate

**Status:** Planned.

**Files:**
- Create: `tests/test_phase3_skill_system_gate.py`
- Modify: `docs/CONTEXT_ENGINEERING_STATUS.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/personal-realtime-ai-assistant-roadmap.md`

**Acceptance:**
- Gate proves repo-local skill manifests can declare permissions and tool mappings.
- Gate proves disabled skills are omitted and audited through load issues.
- Gate proves skills with unavailable tools or missing permissions do not reach prompt context.
- Gate proves no `run_skill` / direct registry execution path exists.
- Roadmap Phase 3 Gate has exact commands.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase3_skill_system_gate.py tests/test_skill_loader.py tests/test_tool_catalog.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase0_tool_governance_contracts.py tests/test_tool_executor.py tests/test_architecture_boundaries.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- AGENTS.md docs src tests skills
```

## Scope Exclusions

- No marketplace.
- No user-uploaded skills.
- No skill review platform.
- No arbitrary code execution.
- No workflow engine.
- No memory schema ownership inside skills.
- No independent skill eval runtime.
- No multi-agent fabric work in this phase.
