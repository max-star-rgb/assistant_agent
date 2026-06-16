# Local Quickstart

This quickstart runs the assistant locally with mock/local defaults.

## Check Environment

```bash
python scripts/check_env.py
```

## Run A Text Prompt

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
```

Expected output includes:

```text
response_text
tool_sequence
run_id
trace_id
errors
offline: true
```

## Run Demo Scenarios

```bash
python scripts/run_demo_flows.py
python scripts/run_demo_flows.py --scenario product_search_compare
```

## Safety Defaults

- Uses mock/local providers by default.
- Does not require API keys.
- Does not require real images or videos.
- Does not call real external Providers.
