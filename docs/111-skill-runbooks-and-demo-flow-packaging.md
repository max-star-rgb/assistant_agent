# 111 Skill Runbooks and Demo Flow Packaging

## Goal

Package repeatable offline workflows as skill runbooks so Codex and local agents can run the project safely.

## Packaged Workflows

### Assistant Demo Flow

Skill:

```text
skills/assistant-demo-flow/SKILL.md
```

Resources:

```text
skills/assistant-demo-flow/resources/demo-scenarios.md
skills/assistant-demo-flow/resources/demo-runbook.md
```

Default commands:

```bash
python scripts/run_demo_flows.py
python scripts/run_demo_flows.py --scenario product_search_compare
```

### Offline MCP Tools

Skill:

```text
skills/offline-mcp-tools/SKILL.md
```

Resources:

```text
skills/offline-mcp-tools/resources/mcp-smoke-runbook.md
skills/offline-mcp-tools/resources/mcp-tool-inventory.md
```

Default commands:

```bash
python scripts/smoke_mcp_tools.py
python -m pytest tests/test_mcp_server_skeleton.py
```

## Safety

Runbooks must:

- use mock/local defaults
- avoid real Provider calls
- avoid real media and generated assets
- avoid API keys and `.env` secrets
- avoid raw Provider outputs
- avoid publishing remote MCP services

## Validation

Use:

```bash
python scripts/validate_skills.py
```

The validator checks frontmatter and obvious unsafe content.
