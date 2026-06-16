---
name: offline-mcp-tools
description: "Validate the local offline MCP skeleton and tool wrappers safely."
version: "1.0.0"
---

# Skill: Offline MCP Tools

## Purpose

Use this skill to validate the Phase 5J offline MCP skeleton.

## Read First

- `AGENTS.md`
- `docs/108-mcp-tool-boundary-contract-inventory.md`
- `docs/109-mcp-server-skeleton.md`
- `skills/offline-mcp-tools/resources/mcp-smoke-runbook.md`
- `skills/offline-mcp-tools/resources/mcp-tool-inventory.md`

## Commands

```bash
python scripts/smoke_mcp_tools.py
python -m pytest tests/test_mcp_server_skeleton.py
```

## Resources

- `resources/mcp-smoke-runbook.md`
- `resources/mcp-tool-inventory.md`

## Safety

- MCP wrappers must use `AgentGraphRuntime` or `ToolRegistry`.
- Do not call Provider SDKs directly.
- Do not publish remote MCP services.
- Do not expose raw trace payloads, raw memory, API keys, Authorization headers, Bearer tokens, full base64, or raw Provider responses.

## Stop Condition

Stop after smoke and tests pass, or after reporting the first failing offline safety check.
