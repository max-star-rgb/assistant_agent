# Troubleshooting

## `python -m pytest` says pytest is missing

Your shell may be using a base Python environment. Use the project environment Python or activate the correct environment first.

Example:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
```

## API does not start

Check the environment:

```bash
python scripts/check_env.py
```

Start locally:

```bash
uvicorn multimodal_agent.api.app:app --host 127.0.0.1 --port 8000 --reload
```

## Docker is unavailable

If `docker --version` returns `command not found`, Docker is not installed in the current environment. Use the local Python run path instead.

## Real Provider returns unconfigured

Confirm the required Provider selector, API key, base URL, and model variables are set in your local shell. See `docs/provider-setup.md`.

## Trace or run cannot be found

Use the same local API process for `POST /agent/run`, `GET /runs/{run_id}`, and `GET /traces/{trace_id}`. In-memory trace state is process-local.

## Demo response looks generic

Run:

```bash
python scripts/run_demo_flows.py
```

The demo runner checks that responses are not the generic `"已完成请求处理。"`.
