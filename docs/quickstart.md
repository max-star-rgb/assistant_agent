# Quickstart

This guide runs the assistant locally with mock/local defaults.

## Check Environment

```bash
python scripts/check_env.py
```

## Run Tests

```bash
python -m pytest
```

## Run The CLI

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
python scripts/run_assistant_cli.py --text "生成一张日系极简海报"
python scripts/run_assistant_cli.py --scenario product_search_compare
```

## Run Demo Flows

```bash
python scripts/run_demo_flows.py
python scripts/run_demo_flows.py --scenario full_multistep_image_search_compare_generate
```

## Run The Local API

```bash
uvicorn multimodal_agent.api.app:app --host 127.0.0.1 --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/demo/console
```

Call the default single-agent endpoint:

```bash
curl -s http://127.0.0.1:8000/agent/run \
  -H 'content-type: application/json' \
  -d '{"user_id":"demo_user","session_id":"demo_session","text":"你好"}'
```

Call the explicit local multi-agent gateway:

```bash
curl -s http://127.0.0.1:8000/agents/run \
  -H 'content-type: application/json' \
  -d '{"user_id":"demo_user","session_id":"demo_session","text":"你好","target_agent_id":"agent.worker","collaboration_mode":"single"}'
```

## Defaults

- No API key required.
- No real Provider called.
- No real image, video, generated image, or render artifact required.
