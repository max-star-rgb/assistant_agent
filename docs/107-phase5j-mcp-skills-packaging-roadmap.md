# 107 Phase 5J Roadmap: MCP / Skills Packaging

## Background

Phase 5I completed Memory Hardening. Phase 5J packages the existing assistant capabilities and stable workflows so they can be reused by local agents, Codex workflows, and offline tooling.

Phase 5J does not add new business capabilities.

## Goals

Phase 5J focuses on:

- MCP tool boundary.
- MCP tool contract inventory.
- MCP server skeleton.
- Offline MCP smoke script.
- `skills/` packaging structure.
- `SKILL.md` templates.
- Skill runbooks and reusable resources.
- MCP / Skills safety validation.
- Docs and review coverage.

## Non-Goals

Phase 5J must not:

- Add new assistant capabilities.
- Add real Providers.
- Call real external Providers by default.
- Publish a remote MCP service.
- Implement complex OAuth or permission systems.
- Bypass `AgentGraphRuntime`, `ToolRegistry`, `CapabilityValidator`, ProviderSafety, or MemoryPrivacy.
- Let MCP tools directly call Provider SDKs.
- Write API keys or `.env` files with secrets.
- Store real user memory, real media, generated images, render artifacts, logs, large files, or raw Provider outputs.

## Default Runtime

Default execution remains local and offline:

```text
MockAdapter / LocalJsonAdapter
InMemoryStore / JsonlMemoryStore
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
```

## Task Sequence

```text
101 Phase 5J MCP / Skills Packaging Roadmap
102 MCP Tool Boundary and Contract Inventory
103 MCP Server Skeleton and Offline Tool Smoke
104 Skills Packaging Structure and Skill Templates
105 Skill Runbooks and Demo Flow Packaging
106 MCP / Skills Safety, Eval, and Docs Coverage
107 Phase 5J Review
```

## Packaging Principles

MCP and Skills are wrappers around stable Agent capabilities. They should expose safe local entry points, not duplicate provider logic.

MCP tools should call:

```text
AgentGraphRuntime / ToolRegistry / existing scripts
```

Skills should provide:

```text
instructions
read-first docs
offline commands
stop conditions
safe runbooks
```

## Completion Criteria

Phase 5J is complete when:

- MCP boundary and contract inventory are documented.
- Offline MCP server skeleton exists.
- Offline MCP smoke passes.
- Skills have YAML frontmatter and safe validation.
- Skill runbooks exist for main workflows.
- Packaging safety tests exist.
- Docs and review report exist.
- Default tests, evals, demo runner, MCP smoke, and skill validation pass offline.
