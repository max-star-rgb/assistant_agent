# Local Deployment

This project supports local deployment for demo and debugging. It is not a production deployment guide.

## Option 1: Local Python

Use the project environment that has test dependencies installed:

```bash
python scripts/check_env.py
python -m pytest
uvicorn multimodal_agent.api.app:app --host 127.0.0.1 --port 8000 --reload
```

Open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/demo/console
```

## Option 2: Docker Compose

```bash
docker compose up --build
```

Open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/demo/console
```

The compose file defaults every Provider selector to mock/local values and keeps `RUN_INTEGRATION_TESTS=0`.

## Smoke Requests

Healthcheck:

```bash
python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health')))"
```

Agent run:

```bash
python -c "import json, urllib.request; payload=json.dumps({'user_id':'local_user','session_id':'local_session','text':'帮我写一段商品介绍'}).encode(); req=urllib.request.Request('http://127.0.0.1:8000/agent/run', data=payload, headers={'content-type':'application/json'}); print(json.load(urllib.request.urlopen(req)))"
```

## Debug Flow

1. Run `POST /agent/run`.
2. Copy `run_id` and `trace_id` from the response.
3. Query `GET /runs/{run_id}`.
4. Query `GET /traces/{trace_id}`.
5. Query `GET /runs/{run_id}/tool-calls`.

More details:

```text
docs/observability-local.md
```

## Boundaries

- No Kubernetes.
- No cloud deployment.
- No production permission model.
- No production monitoring stack.
- No real Provider calls by default.
