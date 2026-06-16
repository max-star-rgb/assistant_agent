# Phase 5J Tasks: MCP / Skills Packaging

Phase 5J packages stable Agent capabilities and workflows for local MCP and Skills usage.

## Order

```text
101 Phase 5J MCP / Skills Packaging Roadmap
102 MCP Tool Boundary and Contract Inventory
103 MCP Server Skeleton and Offline Tool Smoke
104 Skills Packaging Structure and Skill Templates
105 Skill Runbooks and Demo Flow Packaging
106 MCP / Skills Safety, Eval, and Docs Coverage
107 Phase 5J Review
```

## Rules

- Do not add new business capabilities.
- Do not add real Providers.
- Do not call real external Providers by default.
- Do not publish a remote MCP service.
- Do not implement complex OAuth or multi-tenant permission systems.
- MCP tools must reuse `AgentGraphRuntime` / `ToolRegistry`.
- MCP tools must not call Provider SDKs directly.
- MCP tools must not bypass ProviderSafety, MemoryPrivacy, or CapabilityValidator.
- Skills must not contain API keys, real user data, real media, logs, raw Provider outputs, or generated assets.
- Every `SKILL.md` must include YAML frontmatter.
- Default tests, evals, demo runner, MCP smoke, and skill validation must run offline.

## Default Acceptance

```bash
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
```
