---
name: phase5i-runner
description: Automatically runs Phase 5I Memory Hardening tasks from Task 094 through Task 100, using local/offline memory stores and stopping before Phase 5J.
version: 1.0.0
---

# Skill: Phase 5I Memory Hardening Runner

## Purpose

This skill lets Codex execute the complete Phase 5I Memory Hardening stage automatically.

Phase 5I scope:

```text
094 Phase 5I Memory Hardening Roadmap
095 Memory Data Model and Store Boundary
096 Memory Retrieval Ranking and Context Builder
097 Memory Write Policy and Lifecycle
098 Memory Privacy and User Isolation
099 Memory Eval / API / Demo Coverage
100 Phase 5I Review
```

Target stop point:

```text
Task 100 Phase 5I Review
```

After Task 100 finishes, Codex must stop and wait for the user.

## When to use

Use this skill when the user says something like:

```text
使用 phase5i-runner skill
自动执行 Phase 5I
从 Task 094 跑到 Task 100
一次性完成 Memory Hardening
```

Do not use this skill for Phase 5J, MCP, Skills packaging, Phase 6, or unrelated development.

## Phase 5I business goal

Phase 5I focuses on Memory Hardening.

The goal is to make memory_retrieval and memory_save more reliable, safer, and more useful for an assistant agent.

The system should better support user requests like:

```text
上次那个商品
之前那张图
我喜欢的风格
刚才生成的海报
之前比较过的商品
把上次那个包放到客厅里渲染
按我喜欢的日系极简风格生成图片
```

## What Phase 5I should do

Implement or improve:

```text
MemoryItem / MemoryQuery / MemorySearchResult
MemoryStore boundary
InMemoryStore / JsonlMemoryStore compatibility
retrieval ranking
memory_context builder
MemoryWritePolicy
lifecycle / delete
privacy / user isolation
memory eval
memory API or runtime-level tests
memory demo coverage
Phase 5I review report
```

## What Phase 5I must not do

Do not:

- connect to a real Vector DB.
- build a complex RAG platform.
- call an external memory service.
- implement MCP Server.
- implement Skills packaging.
- add real Providers.
- call real external Providers.
- write API keys.
- create `.env` or `.env.local` containing secrets.
- submit real user memory.
- submit real media files.
- submit provider raw responses.
- write outside the repository.
- modify shell profile or global config.
- run destructive commands.

Default runtime must remain:

```text
InMemoryStore / JsonlMemoryStore
MockAdapter / LocalJsonAdapter
offline pytest
offline eval
offline demo runner
```

## Execution mode

This skill temporarily overrides the usual rule:

```text
每次只执行一个 task
```

Instead, Codex should execute Task 094 through Task 100 sequentially.

Rules:

- Finish one task before starting the next.
- Respect each task's Scope.
- Do not expand beyond the current task.
- Run each task's acceptance checks before moving on.
- If a test fails, fix only failures caused by the current or immediately preceding task.
- If a failure appears unrelated, document it and stop.
- Stop after Task 100 completes.
- Do not begin Phase 5J automatically.

## Read first

Before starting, read:

```text
AGENTS.md
docs/99-phase5i-memory-hardening-roadmap.md
tasks/README_PHASE5I.md
```

Then follow each task's own Read first section.

If a file is missing, locate the closest matching Phase 5I file by task number or document title.

## Task sequence

### Task 094 Phase 5I Memory Hardening Roadmap

Read:

```text
docs/99-phase5i-memory-hardening-roadmap.md
tasks/README_PHASE5I.md
```

Goal:

```text
Confirm Phase 5I scope and update stage documentation.
```

Run:

```bash
python -m pytest
```

Then continue to Task 095.

### Task 095 Memory Data Model and Store Boundary

Read:

```text
docs/100-memory-data-model-and-store-boundary.md
```

Goal:

```text
Unify MemoryItem, MemoryQuery, MemorySearchResult, and MemoryStore interface.
```

Run:

```bash
python -m pytest
python scripts/run_evals.py
```

Then continue to Task 096.

### Task 096 Memory Retrieval Ranking and Context Builder

Read:

```text
docs/101-memory-retrieval-ranking-context.md
```

Goal:

```text
Improve local retrieval ranking and memory_context construction.
```

Run:

```bash
python -m pytest
python scripts/run_evals.py
```

Then continue to Task 097.

### Task 097 Memory Write Policy and Lifecycle

Read:

```text
docs/102-memory-write-policy-and-lifecycle.md
```

Goal:

```text
Define what memory should be saved, when it should be saved, and how lifecycle/delete works.
```

Run:

```bash
python -m pytest
python scripts/run_evals.py
```

Then continue to Task 098.

### Task 098 Memory Privacy and User Isolation

Read:

```text
docs/103-memory-privacy-user-isolation.md
```

Goal:

```text
Ensure memory search/save/delete is isolated by user_id and sensitive content is redacted.
```

Run:

```bash
python -m pytest
python scripts/run_evals.py
```

Then continue to Task 099.

### Task 099 Memory Eval / API / Demo Coverage

Read:

```text
docs/104-memory-eval-api-demo-plan.md
```

Goal:

```text
Add memory eval, API/runtime-level tests, and demo runner coverage.
```

Run:

```bash
python scripts/run_evals.py
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
python -m pytest
```

Then continue to Task 100.

### Task 100 Phase 5I Review

Read:

```text
docs/105-phase5i-memory-hardening-review-checklist.md
```

Goal:

```text
Generate the Phase 5I review report.
```

Generate:

```text
docs/106-phase5i-memory-hardening-review.md
```

Run:

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
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
python scripts/run_evals.py --suite memory
python scripts/run_demo_flows.py
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
```

## Memory safety rules

Memory must not store:

```text
API Key
Authorization header
Bearer token
cookie
password
secret
complete base64
complete raw provider response
raw media files
large file contents
cross-user data
```

Memory may store safe summaries and references:

```text
preference summary
task summary
product summary
artifact output_ref
image/video understanding summary
render output_ref
```

All memory search/save/delete operations must respect user_id.

If user_id is missing, use the project's existing safe local/single-user behavior, but do not introduce cross-user leakage.

## Required final response

After Task 100 completes, respond with:

```text
Phase 5I Memory Hardening is complete.

Summary:
- Memory data model:
- MemoryStore boundary:
- Retrieval ranking / context:
- Write policy / lifecycle:
- Privacy / user isolation:
- Eval / API / demo coverage:
- Tests run:
- Remaining issues:
- Recommended next phase:
```

Do not start Phase 5J automatically.
