# Scripts Entry Map

This directory has many operator, smoke, eval, and provider probe scripts. The
current product direction is the Personal Realtime Assistant Runtime, so only a
small subset should be treated as primary entry scripts.

## Primary realtime runtime entries

Use these when validating the realtime assistant loop:

- `scripts/run_server.py`: starts the FastAPI backend with Gateway, media, HTTP,
  memory, trace, and tool-governed runtime routes. For local Tavily web search/fetch
  development, pass `--start-web-search-relay` to start the relay child process
  and wire `WEB_SEARCH_BASE_URL` for this run.
- `scripts/realtime_media_client.py`: server-backed Media Relay protocol smoke
  client and manual text-call operator for `/ws/realtime/media`.
- `scripts/run_client.py`: server-backed Media-Agent protocol console client for
  `/agent-service/v1`; type text repeatedly, or use `/new [sessionId]` to open a
  new media session. Agent chat responses print only the reply text, not the
  raw vendor envelope. The handshake marks `clientInfo.clientType=run_client`
  so trace and Gateway metadata can distinguish local protocol tests from
  ordinary media-agent calls.
- `scripts/run_gateway_client.py`: server-backed normalized Gateway frame smoke
  client for `/ws/gateway`.
- `scripts/run_realtime_call_simulator.py`: in-process text-only realtime gate
  for `basic`, `interrupt`, `hangup`, `cancel`, and `tool_interrupt` lifecycle
  scenarios.

For process-level keepalive, `deploy/supervisord/assistant-agent.conf` can run
`scripts/run_server.py` under `supervisord` and restart it after crashes.

## Not primary product entries

These are useful, but they are not the main product path:

- `scripts/run_assistant_cli.py`: local in-process offline developer smoke.
- `scripts/run_demo_flows.py`: offline scenario matrix for regression demos.
- `scripts/run_evals.py`: eval harness for lower-layer behavior checks.
- `scripts/check_env.py`: environment sanity check.
- `scripts/run_tavily_search_relay.py`: opt-in local HTTP relay that adapts the
  generic `web_search` and `web_fetch` HTTP protocols to Tavily Search/Extract APIs.
- `scripts/check_pilot_readiness.py` and `scripts/collect_pilot_evidence.py`:
  multi-agent pilot operator helpers.
- `scripts/memory_audit.py`, `scripts/agentruntime_view.py`, and
  `scripts/trace_metrics.py`: operator inspection utilities.
- `scripts/smoke_*.py`, `scripts/measure_deepseek_latency.py`, and
  `scripts/haodanku_order_query.mjs`: opt-in provider or domain smoke probes.

Do not add a new general chat entry unless it is explicitly scoped behind the
same Gateway/runtime boundary and the realtime assistant loop is already stable.
