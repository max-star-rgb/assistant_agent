# 112 MCP / Skills Safety Eval Plan

## Goal

Add offline validation coverage for Phase 5J packaging artifacts.

## Packaging Eval Suite

The eval suite is:

```bash
python scripts/run_evals.py --suite packaging
```

It covers:

- skills validation
- MCP tool inventory
- MCP smoke / redaction

## Safety Checks

Packaging checks must confirm:

- MCP tools are backed by `AgentGraphRuntime` / `ToolRegistry`.
- MCP tools do not call Provider SDKs directly.
- MCP outputs are redacted.
- Skills have YAML frontmatter.
- Skills do not contain obvious secrets.
- Skills do not instruct remote MCP publishing.
- All validation runs offline.

## Default Commands

```bash
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
python scripts/run_evals.py --suite packaging
python -m pytest
```

## Non-Goals

This phase does not:

- publish remote MCP services
- install MCP dependencies
- add real Providers
- call real external APIs
- implement production OAuth
- package generated media or logs
