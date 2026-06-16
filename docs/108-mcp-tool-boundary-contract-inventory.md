# 108 MCP Tool Boundary and Contract Inventory

## Goal

Define which existing assistant capabilities can be safely exposed through an offline MCP skeleton, and document the input/output contracts.

Phase 5J MCP tools are wrappers around the existing runtime. They must not directly call Provider SDKs.

## Boundary

MCP tools may call:

- `AgentGraphRuntime`
- `ToolRegistry`
- existing mock/local tools
- existing offline scripts

MCP tools must respect:

- `CapabilityValidator`
- ProviderSafety / ProviderCallBudget / retry and timeout policy
- MemoryPrivacy / MemoryWritePolicy
- capability output contracts
- trace redaction

MCP tools must not:

- call real Provider SDKs directly
- bypass `AgentGraphRuntime`
- publish a remote service
- upload media to external Providers by default
- expose local sensitive paths
- expose raw trace payloads
- write memory without existing memory policy

## Exposed MCP Tool Candidates

### `agent_run`

Runs the assistant through `AgentGraphRuntime`.

Input:

```json
{
  "user_id": "u1",
  "session_id": "s1",
  "text": "帮我找白色运动鞋并比价",
  "image_ids": [],
  "video_ids": [],
  "metadata": {}
}
```

Output:

```text
AgentResponse summary
tool sequence
run_id
trace_id
errors
output_refs
```

Safety:

- Uses default mock/local configuration.
- Does not call real Providers by default.
- Redacts errors via existing API/trace safety layers.

### `tool_list`

Lists registered tools from `ToolRegistry`.

Input:

```json
{}
```

Output:

```json
{
  "tools": ["image_generation", "memory_retrieval", "..."]
}
```

Safety:

- Does not execute tools.
- Does not expose Provider config or secrets.

### `tool_run`

Runs one registered local tool through `ToolRegistry`.

Input:

```json
{
  "tool_name": "product_search",
  "input": {"query": "白色运动鞋"}
}
```

Output:

```text
ToolResult
```

Safety:

- Uses `ToolRegistry.run()`.
- Default registry uses mock/local adapters.
- Returned payload must be sanitized and must not include raw Provider responses.

### `demo_flow_run`

Runs one existing offline demo scenario through `scripts/run_demo_flows.py` logic.

Input:

```json
{
  "scenario_id": "product_search_compare"
}
```

Output:

```text
offline demo summary
```

Safety:

- Uses demo scenario matrix.
- Does not call real Providers.
- Does not emit secrets or raw media.

## Not Exposed in Phase 5J

Do not expose:

- direct Provider SDK calls
- real Provider smoke scripts
- key/config mutation
- remote server publish/deploy commands
- raw trace dump
- raw memory dump
- filesystem reads outside safe repo docs/resources
- login, cookies, purchases, payments

## Capability Inventory

| Capability | Tool | MCP Exposure | Input | Output | Notes |
| --- | --- | --- | --- | --- | --- |
| `direct_chat` | runtime node | `agent_run` only | text | `AgentResponse` | No direct Provider SDK |
| `image_generation` | `image_generation` | `agent_run`, `tool_run` | text, optional refs | `ImageGenerationResult` | Default mock/local |
| `image_understanding` | `vision_understanding` | `agent_run`, `tool_run` | image refs | `VisualUnderstandingResult` | No raw image body |
| `video_understanding` | `video_understanding` | `agent_run`, `tool_run` | video ref | `VideoUnderstandingResult` | No raw video body |
| `product_search` | `product_search` | `agent_run`, `tool_run` | query/context | `ProductSearchResult` | No crawling/login/payment |
| `price_compare` | `price_compare` | `agent_run`, `tool_run` | product candidates/query | `PriceCompareResult` | No purchase action |
| `render_3d` | `render_3d` | `agent_run`, `tool_run` | scene/context | `RenderResult` | Mock render only |
| `memory_retrieval` | `memory_retrieval` | `agent_run`, `tool_run` | user_id/query | `MemorySearchResult` | User isolated |
| `memory_save` | `memory_save` | `agent_run`, `tool_run` | explicit content | `MemoryItem` | Uses memory policy |
| `multi_step_orchestration` | runtime plan | `agent_run` only | text/media refs | `AgentResponse` | Planner/validator required |

## Contract Shape

All MCP tool responses should use a stable envelope:

```json
{
  "status": "succeeded",
  "tool": "agent_run",
  "data": {},
  "errors": [],
  "metadata": {
    "offline": true
  }
}
```

Errors should use:

```json
{
  "code": "mcp_tool_failed",
  "message": "safe redacted message",
  "recoverable": false
}
```

## Validation

Task 103 will create the offline MCP skeleton and smoke script that checks:

- tools can be listed
- `agent_run` works with mock/local runtime
- `tool_run` works for a safe mock/local tool
- outputs are redacted
- no real Provider is called
