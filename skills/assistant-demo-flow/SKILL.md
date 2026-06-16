---
name: assistant-demo-flow
description: "Run and inspect offline assistant demo flows using mock/local providers only."
version: "1.0.0"
---

# Skill: Assistant Demo Flow

## Purpose

Use this skill to run local demo scenarios for the multimodal assistant without calling real Providers.

## Read First

- `AGENTS.md`
- `docs/73-e2e-demo-runner.md`
- `demo_data/scenarios/e2e_demo_scenarios.json`
- `skills/assistant-demo-flow/resources/demo-runbook.md`
- `skills/assistant-demo-flow/resources/demo-scenarios.md`

## Commands

```bash
python scripts/run_demo_flows.py
python scripts/run_demo_flows.py --scenario product_search_compare
```

## Resources

- `resources/demo-runbook.md`
- `resources/demo-scenarios.md`

## Safety

- Use mock/local defaults.
- Do not write API keys.
- Do not add real media or generated assets.
- Do not call real Providers.
- Do not publish output logs containing secrets.

## Stop Condition

Stop after reporting demo summary, failed scenario IDs, and any offline validation errors.
