---
name: phase5j-runner
description: "Automatically runs Phase 5J MCP / Skills Packaging tasks from Task 101 through Task 107, using offline mock/local boundaries and stopping before Phase 6."
version: "1.0.0"
---

# Skill: Phase 5J MCP / Skills Packaging Runner

## Purpose

This skill lets Codex execute the complete Phase 5J MCP / Skills Packaging stage automatically.

Phase 5J scope:

```text
101 Phase 5J MCP / Skills Packaging Roadmap
102 MCP Tool Boundary and Contract Inventory
103 MCP Server Skeleton and Offline Tool Smoke
104 Skills Packaging Structure and Skill Templates
105 Skill Runbooks and Demo Flow Packaging
106 MCP / Skills Safety, Eval, and Docs Coverage
107 Phase 5J Review
```

Target stop point:

```text
Task 107 Phase 5J Review
```

After Task 107 finishes, Codex must stop and wait for the user.

## When to use

Use this skill when the user says something like:

```text
使用 phase5j-runner skill
自动执行 Phase 5J
从 Task 101 跑到 Task 107
一次性完成 MCP / Skills Packaging
```

Do not use this skill for Phase 6 or unrelated development.

## Phase 5J business goal

Phase 5J does not add new assistant capabilities. It packages already stable capabilities and workflows into:

```text
MCP tool skeletons
Skills
Runbooks
Offline smoke scripts
Safety validation
```

The purpose is to make the project easier to reuse by external agents, Codex workflows, and local tooling.

## What Phase 5J should do

Implement or improve:

```text
MCP tool boundary
MCP tool contract inventory
MCP server skeleton
offline MCP smoke
skills/ directory structure
SKILL.md templates
skill runbooks
MCP / Skills safety checks
MCP / Skills docs coverage
Phase 5J review report
```

## What Phase 5J must not do

Do not:

- add new business capabilities.
- add real Providers.
- call real external Providers.
- publish a remote MCP service.
- implement production OAuth.
- implement complex multi-tenant permissions.
- install dependencies from the network.
- write API keys.
- create `.env` or `.env.local` containing secrets.
- submit real user memory.
- submit real media files.
- submit generated images or rendered assets.
- submit raw Provider responses.
- bypass ProviderSafety.
- bypass MemoryPrivacy.
- bypass CapabilityValidator.
- let MCP tools directly call Provider SDKs.
- write outside the repository.
- modify shell profile or global config.
- run destructive commands.

Default runtime must remain:

```text
MockAdapter / LocalJsonAdapter
InMemoryStore / JsonlMemoryStore
offline pytest
offline eval
offline demo runner
offline MCP smoke
offline skills validation
```

## Execution mode

This skill temporarily overrides the usual rule:

```text
每次只执行一个 task
```

Instead, Codex should execute Task 101 through Task 107 sequentially.

Rules:

- Finish one task before starting the next.
- Respect each task's Scope.
- Do not expand beyond the current task.
- Run each task's acceptance checks before moving on.
- If a test fails, fix only failures caused by the current or immediately preceding task.
- If a failure appears unrelated, document it and stop.
- Stop after Task 107 completes.
- Do not begin Phase 6 automatically.

## Read first

Before starting, read:

```text
AGENTS.md
docs/107-phase5j-mcp-skills-packaging-roadmap.md
tasks/README_PHASE5J.md
```

Then follow each task's own Read first section.

If a file is missing, locate the closest matching Phase 5J file by task number or document title.

## Task sequence

### Task 101 Phase 5J MCP / Skills Packaging Roadmap

Read:

```text
docs/107-phase5j-mcp-skills-packaging-roadmap.md
tasks/README_PHASE5J.md
```

Goal:

```text
Confirm Phase 5J scope and update stage documentation.
```

Run:

```bash
python -m pytest
```

Then continue to Task 102.

### Task 102 MCP Tool Boundary and Contract Inventory

Read:

```text
docs/108-mcp-tool-boundary-contract-inventory.md
```

Goal:

```text
Define which internal capabilities can safely be exposed as MCP tools and document their input/output contracts.
```

Run:

```bash
python -m pytest
```

Then continue to Task 103.

### Task 103 MCP Server Skeleton and Offline Tool Smoke

Read:

```text
docs/109-mcp-server-skeleton.md
```

Goal:

```text
Create a lightweight MCP server skeleton and offline smoke script.
```

Run:

```bash
python scripts/smoke_mcp_tools.py
python -m pytest
```

If `scripts/smoke_mcp_tools.py` does not exist before the task, create it as part of the task.

Then continue to Task 104.

### Task 104 Skills Packaging Structure and Skill Templates

Read:

```text
docs/110-skills-packaging-structure.md
```

Goal:

```text
Create the skills/ directory structure and SKILL.md templates.
```

Run:

```bash
python scripts/validate_skills.py
python -m pytest
```

If `scripts/validate_skills.py` does not exist before the task, create it as part of the task.

Then continue to Task 105.

### Task 105 Skill Runbooks and Demo Flow Packaging

Read:

```text
docs/111-skill-runbooks-and-demo-flow-packaging.md
```

Goal:

```text
Add runbooks and reusable resources for the main skills.
```

Run:

```bash
python scripts/validate_skills.py
python -m pytest
```

Then continue to Task 106.

### Task 106 MCP / Skills Safety, Eval, and Docs Coverage

Read:

```text
docs/112-mcp-skills-safety-eval-plan.md
```

Goal:

```text
Add offline safety validation, tests, docs coverage, and optional packaging eval.
```

Run:

```bash
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
python -m pytest
```

If `python scripts/run_evals.py --suite packaging` is implemented, run it too.

Then continue to Task 107.

### Task 107 Phase 5J Review

Read:

```text
docs/113-phase5j-mcp-skills-review-checklist.md
```

Goal:

```text
Generate the Phase 5J review report.
```

Generate:

```text
docs/114-phase5j-mcp-skills-review.md
```

Run:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```

Then stop.

## Editing rules

Use `apply_patch` for manual source, test, and documentation edits.

Do not default to:

```bash
python -c "Path(...).write_text(...)"
cat > file <<'EOF'
sed -i
perl -pi
```

Allowed exceptions:

- small verification scripts
- environment checks
- safe mechanical transformations when clearly justified

If `apply_patch` fails, explain why before using another method.

## Command rules

Codex may run these without asking:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
ruff check
ruff format
mypy
```

Codex must ask before:

```text
retry without sandbox
network access
installing dependencies
using sudo
using docker
modifying files outside the repository
deleting large file sets
git push
publishing MCP services
```

## MCP safety rules

MCP tools must:

```text
reuse AgentGraphRuntime / ToolRegistry
respect ProviderSafety
respect MemoryPrivacy
respect CapabilityValidator
default to mock/local
avoid direct Provider SDK calls
avoid remote publication
avoid raw Provider responses
```

MCP tools must not:

```text
directly call real Provider SDKs
bypass existing runtime safeguards
write memory without explicit policy
upload media to real Providers by default
expose local sensitive paths
expose raw trace payloads
```

## Skills safety rules

Skills must:

```text
contain SKILL.md with YAML frontmatter
contain clear Read first sections
contain validation commands
contain stop conditions
avoid secrets
avoid real user data
avoid raw Provider outputs
avoid network-only workflows
respect AGENTS.md
```

Every generated `SKILL.md` must begin with YAML frontmatter:

```markdown
---
name: example-skill
description: "Short description."
version: "1.0.0"
---
```

## Required final response

After Task 107 completes, respond with:

```text
Phase 5J MCP / Skills Packaging is complete.

Summary:
- MCP boundary:
- MCP server skeleton:
- MCP tool inventory:
- Skills packaging:
- Runbooks:
- Safety / smoke / tests:
- Tests run:
- Remaining issues:
- Recommended next phase:
```

Do not start Phase 6 automatically.
