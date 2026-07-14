# Memory framework bake-off operator runbook

This runbook executes the opt-in Hindsight `0.8.4` versus Mem0 OSS `2.0.11` comparison. It is operational guidance; `docs/memory-service-architecture.md` remains authoritative for boundaries.

## Safety prerequisites

- Use only `MULTIMODAL_AGENT_RUNTIME_PROFILE=pilot` or `provider_smoke` for a real run.
- Put chat and embedding credentials in the shell or an untracked secure environment file. Never write them into this repository or a report.
- Use the same OpenAI-compatible chat model, embedding model, base URLs, budgets, corpus, host, and Docker resource limits for both frameworks.
- Do not run both profiles against the same volume. The named volumes are intentionally separate.
- Back up the existing v2 database before switching configuration. This phase starts framework databases empty and does not migrate or dual-write old memory.

Required shell variables:

```bash
export MEMORY_BAKEOFF_CHAT_BASE_URL='https://provider.example/v1'
export MEMORY_BAKEOFF_CHAT_MODEL='fixed-chat-model'
export MEMORY_BAKEOFF_CHAT_API_KEY='set-in-shell-only'
export MEMORY_BAKEOFF_EMBEDDING_BASE_URL='https://provider.example/v1'
export MEMORY_BAKEOFF_EMBEDDING_MODEL='fixed-embedding-model'
export MEMORY_BAKEOFF_EMBEDDING_API_KEY='set-in-shell-only'
```

## Validate and start one sidecar

The Hindsight image and Qdrant image use fixed tags. The Mem0 image is built locally with `mem0ai==2.0.11` because the official API-server registry does not publish a `2.0.11` image tag; the main assistant environment remains dependency-free.

```bash
docker compose -f docker/memory-frameworks/compose.yaml config --quiet
docker compose -f docker/memory-frameworks/compose.yaml --profile hindsight up -d hindsight
curl --fail http://127.0.0.1:8889/health
```

Stop Hindsight before starting Mem0 under the same controlled host budget:

```bash
docker compose -f docker/memory-frameworks/compose.yaml --profile hindsight down
docker compose -f docker/memory-frameworks/compose.yaml --profile mem0 up -d --build mem0
curl --fail http://127.0.0.1:8890/
```

Framework runtime configuration is explicit:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=pilot
export MULTIMODAL_AGENT_MEMORY_BACKEND=framework
export MULTIMODAL_AGENT_MEMORY_FRAMEWORK_ENABLED=true
export MULTIMODAL_AGENT_MEMORY_FRAMEWORK=hindsight
export MEMORY_FRAMEWORK_VERSION=0.8.4
export MEMORY_FRAMEWORK_BASE_URL=http://127.0.0.1:8889
export MEMORY_FRAMEWORK_LEDGER_PATH=.local/memory/hindsight-governance.sqlite3
export MEMORY_FRAMEWORK_FALLBACK_BACKEND=none
```

For Mem0, change the framework to `mem0`, version to `2.0.11`, URL to `http://127.0.0.1:8890`, and use a separate ledger path.

## Collect evidence

Use the same fixed evaluation corpus for both runs. Record Recall@5, MRR, write precision, contradiction update, temporal, multi-hop, Chinese, episodic/procedural, and false-positive results. Run explicit cross-tenant/user/project/session probes and require zero leakage. Exercise retain, recall, get/list, history, export, delete, clear, confirmation, restart recovery, and outbox recovery through `MemoryManager` or `ToolExecutor`; direct adapter probes are allowed only for adapter contract evidence.

Record p95 retain/recall latency after warm-up, cold-start time, peak RSS, persistent-volume disk use, restart recovery, configuration step count, and backup portability. Useful host commands are:

```bash
docker stats --no-stream
docker system df -v
docker compose -f docker/memory-frameworks/compose.yaml restart hindsight
docker compose -f docker/memory-frameworks/compose.yaml restart mem0
```

Create one JSON file per framework matching `FrameworkBakeoffMetrics`. Print the exact JSON Schema without writing provider data:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c 'import json; from assistant_agent.memory.framework.bakeoff import FrameworkBakeoffMetrics; print(json.dumps(FrameworkBakeoffMetrics.model_json_schema(), ensure_ascii=False, indent=2))'
```

Score both measured files:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_memory_framework_bakeoff.py \
  --hindsight-metrics /secure/path/hindsight-metrics.json \
  --mem0-metrics /secure/path/mem0-metrics.json \
  --output /secure/path/memory-framework-bakeoff-report.json
```

The scorer is deterministic and omits timestamps so identical inputs produce identical reports. Keep raw provider responses and credentials out of metric files. Preserve both measured inputs and the scored report in the approved evidence location.

## Selection and rollback

Only remove the losing runtime adapter after the report names an eligible winner. A winner remains explicit opt-in; CI and default profiles stay mock/local/offline. If neither framework passes every hard gate, leave v2 as the recommendation and keep framework mode disabled.

Before a production switch, stop writes, back up the v2 database, start the winner from an empty framework volume, and retain the old database as the rollback point. Rollback changes only runtime configuration back to `sqlite` or the previous v2 backend; it does not merge framework and v2 data.
