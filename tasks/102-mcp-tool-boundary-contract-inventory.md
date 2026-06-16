# Task 102 MCP Tool Boundary and Contract Inventory

## Goal

Define which internal capabilities can safely be exposed as MCP tools and document their input/output contracts.

## Read first

- `docs/108-mcp-tool-boundary-contract-inventory.md`
- `docs/05-tool-contracts.md`
- current tool registry
- current capability contracts

## Scope

- Document safe MCP tool candidates.
- Document blocked/non-goal tool exposure.
- Add a lightweight contract inventory.
- Do not implement MCP server yet.

## Acceptance

```bash
python -m pytest
```
