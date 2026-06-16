# MCP Tool Inventory

MCP-visible tools:

- `agent_run`
- `tool_list`
- `tool_run`
- `demo_flow_run`

Internal backing:

- `AgentGraphRuntime`
- `ToolRegistry`
- existing demo flow runner

Blocked in Phase 5J:

- direct Provider SDK calls
- remote MCP publication
- raw trace dump
- raw memory dump
- API key or `.env` mutation
- real Provider smoke calls
