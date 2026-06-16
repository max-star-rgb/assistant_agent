# 109 MCP Server Skeleton

## Goal

Provide a lightweight local MCP skeleton for offline validation. Phase 5J does not publish a remote MCP service and does not depend on a network MCP runtime.

## Design

The skeleton lives under:

```text
src/multimodal_agent/mcp/server.py
```

It provides a small in-process interface:

```python
server = OfflineMCPServer()
server.list_tools()
server.call_tool("agent_run", {...})
```

## Supported Tools

### `agent_run`

Runs `AgentGraphRuntime` with a `UserRequest`.

### `tool_list`

Lists MCP-visible tools.

### `tool_run`

Runs a registered local tool through `ToolRegistry`.

### `demo_flow_run`

Runs one offline demo scenario through existing demo runner logic.

## Safety

The skeleton:

- Uses default `ProviderConfig()` so all providers are mock/local.
- Reuses `AgentGraphRuntime` and `ToolRegistry`.
- Does not call Provider SDKs directly.
- Does not publish remote endpoints.
- Sanitizes response payloads.
- Blocks direct key/config mutation tools.

## Smoke

The smoke script:

```bash
python scripts/smoke_mcp_tools.py
```

validates:

- MCP tool listing works.
- `agent_run` works offline.
- `tool_run` works for a mock/local tool.
- error outputs are redacted.
- no real Provider is called.
