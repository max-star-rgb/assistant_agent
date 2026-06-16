# Offline MCP Smoke Runbook

## Read First

- `docs/108-mcp-tool-boundary-contract-inventory.md`
- `docs/109-mcp-server-skeleton.md`

## Smoke Command

```bash
python scripts/smoke_mcp_tools.py
```

Expected result:

```text
ok = true
offline = true
tool_list succeeds
agent_run succeeds
tool_run succeeds
redaction case fails safely
```

## Focus Checks

- MCP wrappers use `AgentGraphRuntime` or `ToolRegistry`.
- No direct Provider SDK calls.
- No remote MCP publish step.
- No API key, Authorization header, Bearer token, full base64, or raw Provider response in output.

## Follow-up Test

```bash
python -m pytest tests/test_mcp_server_skeleton.py
```
