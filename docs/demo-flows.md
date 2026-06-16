# Demo Flows

Demo flows are offline and reproducible.

## Scenario Matrix

The scenario matrix lives at:

```text
demo_data/scenarios/e2e_demo_scenarios.json
```

It covers text chat, image generation, image/video understanding, product search, price compare, render, memory, and multi-step flows.

## Run All Scenarios

```bash
python scripts/run_demo_flows.py
```

## Run One Scenario

```bash
python scripts/run_demo_flows.py --scenario product_search_compare
python scripts/run_demo_flows.py --scenario full_multistep_image_search_compare_generate
```

## CLI Scenario Mode

```bash
python scripts/run_assistant_cli.py --scenario product_search_compare
```

## Web Console

Start the API and open:

```text
http://127.0.0.1:8000/demo/console
```

## Safety

Demo flows use mock/local refs and do not require real media or real Providers.
