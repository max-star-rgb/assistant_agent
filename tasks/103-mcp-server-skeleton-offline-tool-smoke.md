# Task 103 MCP Server Skeleton and Offline Tool Smoke

## Goal

Create a lightweight MCP server skeleton and offline smoke script.

## Read first

- `docs/109-mcp-server-skeleton.md`
- `docs/108-mcp-tool-boundary-contract-inventory.md`
- current AgentGraphRuntime
- current ToolRegistry

## Scope

- Add local MCP skeleton code.
- Add offline smoke script.
- Keep default mock/local.
- Do not publish remote MCP service.
- Do not call real Providers.

## Acceptance

```bash
python scripts/smoke_mcp_tools.py
python -m pytest
```
