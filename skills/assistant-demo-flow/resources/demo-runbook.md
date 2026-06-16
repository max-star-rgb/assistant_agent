# Assistant Demo Flow Runbook

## Read First

- `AGENTS.md`
- `docs/73-e2e-demo-runner.md`
- `demo_data/scenarios/e2e_demo_scenarios.json`

## Run All Offline Demo Flows

```bash
python scripts/run_demo_flows.py
```

Expected result:

```text
failed = 0
all scenarios use mock/local providers
```

## Run One Scenario

```bash
python scripts/run_demo_flows.py --scenario product_search_compare
python scripts/run_demo_flows.py --scenario memory_product_to_render
```

## Report

Summarize:

- total scenarios
- failed scenario IDs
- tool sequence for failed cases
- whether response text is non-generic

## Safety

- Do not set Provider API keys for this runbook.
- Do not attach real media files.
- Do not persist smoke output as logs.
- Do not include raw Provider output.
