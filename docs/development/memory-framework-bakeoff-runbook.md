# Memory framework bake-off operator runbook

This runbook executes the opt-in Hindsight `0.8.4` versus Mem0 OSS `2.0.11` comparison. It is operational guidance; `docs/memory-service-architecture.md` remains authoritative for boundaries.

## Safety prerequisites

- Use only `MULTIMODAL_AGENT_RUNTIME_PROFILE=pilot` or `provider_smoke` for a real run.
- Put chat and embedding credentials in the shell or an untracked secure environment file. Never write them into this repository or a report.
- Use the same OpenAI-compatible chat model, embedding model, base URLs, budgets, corpus, host, and Docker resource limits for both frameworks.
- Do not run both profiles against the same volume. The named volumes are intentionally separate.
- Back up the existing v2 database before switching configuration. This phase starts framework databases empty and does not migrate or dual-write old memory.

The collector fixes provider endpoints and models internally. Set only the
explicit real-provider profile and one shell-only Alibaba Cloud Model Studio
key:

```bash
export MULTIMODAL_AGENT_RUNTIME_PROFILE=provider_smoke
export MEMORY_BAKEOFF_API_KEY='set-in-shell-only'
```

The fixed values are:

- OpenAI-compatible base URL: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- chat model: `qwen-plus`
- embedding model: `text-embedding-v4` with 1024 dimensions

The CLI maps `MEMORY_BAKEOFF_API_KEY` to both chat and embedding variables for
Compose children. Do not put it in `.env`, command arguments, evidence, or a
report.

## Validate the local runtime

The Hindsight base image is fixed to the published `0.8.4` digest. A local
derived image pre-caches its unchanged default
`cross-encoder/ms-marco-MiniLM-L-6-v2` model so runtime startup does not depend
on a Hugging Face download; Hindsight code and reranker selection are not
changed. The Qdrant image uses a fixed tag. The Mem0 image is built locally
with `mem0ai==2.0.11` because the official API-server registry does not publish
a `2.0.11` image tag; the main assistant environment remains dependency-free.
Hindsight's OpenAI-compatible embedding variables use the required
`HINDSIGHT_API_EMBEDDINGS_OPENAI_*` names. Mem0 and Qdrant both fix the
embedding dimension to 1024. Hindsight retain completion is capped at `32768`,
the maximum accepted by `qwen-plus`, instead of the image default of `64000`.

```bash
docker version
docker compose version
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/collect_memory_framework_bakeoff.py --help
```

The collector validates the rendered Compose configuration with the fixed
provider values, removes the dedicated project volumes, starts only the
selected profile, waits for localhost health, and keeps its governance ledger
in a temporary runtime directory. Manual sidecar startup and framework backend
environment switching are not required for evidence collection. Hindsight is
given a 900-second first-start budget because initializing pg0 from an empty
volume can exceed the image's default five-minute startup window; Mem0 keeps a
600-second budget because its readiness endpoint initializes both the primary
and migration Qdrant collections before quality cases begin. The Mem0 sidecar
serializes singleton initialization so health retries cannot race each other.

## Collect evidence

Use the collector in this exact order. Every invocation starts from newly
created dedicated Compose volumes. A smoke failure exits with code 2 and a
stable `memory_bakeoff_*` error code; stop immediately and do not proceed to a
paid full run.

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/collect_memory_framework_bakeoff.py \
  --phase smoke \
  --framework hindsight \
  --evidence-dir .local/memory-framework-bakeoff

/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/collect_memory_framework_bakeoff.py \
  --phase smoke \
  --framework mem0 \
  --evidence-dir .local/memory-framework-bakeoff

/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/collect_memory_framework_bakeoff.py \
  --phase full \
  --framework hindsight \
  --evidence-dir .local/memory-framework-bakeoff

/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/collect_memory_framework_bakeoff.py \
  --phase full \
  --framework mem0 \
  --evidence-dir .local/memory-framework-bakeoff
```

The full phase uses the same fixed 50 cases for both frameworks and records
Recall@5, MRR, write precision, contradiction update, temporal, multi-hop,
Chinese, episodic/procedural, and false-positive results. It also runs explicit
cross-tenant/user/project/session probes and requires zero leakage. Retain,
recall, get/list, export, delete, clear, confirmation, restart recovery, and
outbox recovery pass through `ToolExecutor` and `MemoryManager`; only health,
adapter history, and Docker resource observations are direct.

Record p95 retain/recall latency after warm-up, cold-start time, peak RSS, persistent-volume disk use, restart recovery, configuration step count, and backup portability. Useful host commands are:

```bash
docker stats --no-stream
docker system df -v
docker compose -f docker/memory-frameworks/compose.yaml restart hindsight
docker compose -f docker/memory-frameworks/compose.yaml restart mem0
```

Each successful invocation writes an anonymous evidence file and a metrics file,
for example `hindsight-full-evidence.json` and
`hindsight-full-metrics.json`. No raw provider response, corpus text, API key,
sidecar URL, or real user data is retained. Print the exact metrics JSON Schema
without writing provider data:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -c 'import json; from assistant_agent.memory.framework.bakeoff import FrameworkBakeoffMetrics; print(json.dumps(FrameworkBakeoffMetrics.model_json_schema(), ensure_ascii=False, indent=2))'
```

Score both measured files:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_memory_framework_bakeoff.py \
  --hindsight-metrics .local/memory-framework-bakeoff/hindsight-full-metrics.json \
  --mem0-metrics .local/memory-framework-bakeoff/mem0-full-metrics.json \
  --output .local/memory-framework-bakeoff/memory-framework-bakeoff-report.json
```

The scorer is deterministic and omits timestamps so identical inputs produce identical reports. Keep raw provider responses and credentials out of metric files. Preserve both measured inputs and the scored report in the approved evidence location.

## Selection and rollback

Only remove the losing runtime adapter after the report names an eligible winner. A winner remains explicit opt-in; CI and default profiles stay mock/local/offline. If neither framework passes every hard gate, leave v2 as the recommendation and keep framework mode disabled.

Before a production switch, stop writes, back up the v2 database, start the winner from an empty framework volume, and retain the old database as the rollback point. Rollback changes only runtime configuration back to `sqlite` or the previous v2 backend; it does not merge framework and v2 data.
