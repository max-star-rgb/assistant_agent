# Scripts Entry Map

This directory has many operator, smoke, eval, and provider probe scripts. The
current product direction is the Personal Realtime Assistant Runtime, so only a
small subset should be treated as primary entry scripts.

## Primary realtime runtime entries

Use these when validating the realtime assistant loop:

- `scripts/run_server.py`: starts the FastAPI backend with Gateway, media, HTTP,
  memory, trace, and tool-governed runtime routes.
- `scripts/realtime_media_client.py`: server-backed Media Relay protocol smoke
  client and manual text-call operator for `/ws/realtime/media`.
- `scripts/run_gateway_client.py`: server-backed normalized Gateway frame smoke
  client for `/ws/gateway`.
- `scripts/run_realtime_call_simulator.py`: in-process text-only realtime gate
  for `basic`, `interrupt`, `hangup`, `cancel`, and `tool_interrupt` lifecycle
  scenarios.

## Not primary product entries

These are useful, but they are not the main product path:

- `scripts/run_assistant_cli.py`: local in-process offline developer smoke.
- `scripts/run_demo_flows.py`: offline scenario matrix for regression demos.
- `scripts/run_evals.py`: eval harness for lower-layer behavior checks.
- `scripts/check_env.py`: environment sanity check.
- `scripts/check_pilot_readiness.py` and `scripts/collect_pilot_evidence.py`:
  multi-agent pilot operator helpers.
- `scripts/memory_audit.py`, `scripts/trace_view.py`, and
  `scripts/trace_metrics.py`: operator inspection utilities.
- `scripts/smoke_*.py`, `scripts/measure_deepseek_latency.py`, and
  `scripts/haodanku_order_query.mjs`: opt-in provider or domain smoke probes.

Do not add a new general chat entry unless it is explicitly scoped behind the
same Gateway/runtime boundary and the realtime assistant loop is already stable.
