# Phase 1 Text Realtime Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first repeatable text-only realtime assistant loop over the existing Media Relay WebSocket and Gateway lifecycle.

**Architecture:** Phase 1 treats ASR and TTS as external media-service responsibilities. This repository only validates text/media-reference events, maps them to Gateway frames, runs the existing `GatewayAgentAdapter -> AgentGraphRuntime` backend, emits text Gateway frames, and records a prompt-safe timeline for debugging.

**Tech Stack:** Python, FastAPI WebSocket tests, existing `GatewaySessionManager`, `/ws/realtime/media`, `GatewayAgentAdapter`, mock/local providers, pytest, existing trace store/debug helpers.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Do not implement ASR, TTS, voice cloning, audio streaming, or audio storage in this repository.
- Do not persist raw audio, base64, provider raw responses, API keys, or real user data.
- Do not add a second agent loop.
- Do not move planning, tool selection, memory policy, provider policy, or agent routing into Gateway or media adapters.
- Realtime traffic must remain `Media Relay event -> Gateway frame -> GatewaySessionManager -> GatewayAgentAdapter -> AgentGraphRuntime`.
- Use `apply_patch` for manual edits.
- Do not add dependencies.

---

## Task 1: Text Media Loop Contract Tests

**Status:** Completed.

**Files:**
- Modify: `tests/test_gateway_api.py`
- Modify: `docs/gateway-architecture.md`

**Acceptance:**
- `/ws/realtime/media` handles `session.start -> transcript.final -> session.end` using text only.
- Returned frames include `call.ready`, `run.started`, at least one text response frame, `run.end`, and `call.hangup_ack`.
- Invalid textless `transcript.final` returns `error` and does not produce `run.started`.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py tests/test_realtime_event_mapping.py tests/test_realtime_media_client_script.py -q
git diff --check -- docs/gateway-architecture.md tests/test_gateway_api.py
```

## Task 2: Text Realtime Call Simulator

**Status:** Completed.

**Files:**
- Create: `scripts/run_realtime_call_simulator.py`
- Create: `tests/test_realtime_call_simulator.py`
- Modify: `docs/gateway-architecture.md`

**Acceptance:**
- `--scenario basic` runs an in-process text call: `session.start`, one or more `transcript.final`, `session.end`.
- `--scenario interrupt` proves explicit interrupt starts a replacement turn and records cancel/interrupt frames.
- Output is JSON with `scenario`, `status`, `frames`, `run_ids`, `trace_ids`, `final_texts`, and `latency_ms`.
- The simulator uses FastAPI `TestClient` or existing Gateway test utilities; it does not require a running server or real provider.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario basic
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario interrupt
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_call_simulator.py -q
git diff --check -- scripts/run_realtime_call_simulator.py tests/test_realtime_call_simulator.py docs/gateway-architecture.md
```

## Task 3: Text Response Edge Contract

**Status:** Completed.

**Files:**
- Modify: `tests/test_realtime_audio_edge.py`
- Modify: `src/assistant_agent/realtime/audio_edge.py` only if existing helper does not expose enough prompt-safe text metadata.

**Acceptance:**
- Speakable text is derived only from `stream.chunk` and display-safe progress frames.
- No TTS provider is invoked.
- No audio bytes or base64 appear in output.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_audio_edge.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
git diff --check -- src/assistant_agent/realtime/audio_edge.py tests/test_realtime_audio_edge.py tests/test_realtime_event_mapping.py
```

## Task 4: Interrupt / Hangup Text Lifecycle Hardening

**Status:** Completed.

**Files:**
- Modify: `tests/test_gateway_session.py`
- Modify: `tests/test_realtime_agent_backend.py`
- Modify: `src/assistant_agent/gateway/bridge.py`
- Modify: `src/assistant_agent/gateway/session.py` only if tests expose drift.

**Acceptance:**
- Ordinary text turns queue behind an active run.
- Explicit `interrupt=true` or `metadata.control=interrupt` cancels active run and starts the new text turn.
- `session.end` cancels active run and emits `call.hangup_ack`.
- Cancel/hangup/interrupt source remains prompt-safe and visible in Gateway frames or trace metadata.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_session.py tests/test_realtime_agent_backend.py tests/test_realtime_task_state.py -q
git diff --check -- src/assistant_agent/gateway/bridge.py src/assistant_agent/gateway/session.py tests/test_gateway_session.py tests/test_realtime_agent_backend.py
```

## Task 5: Phase 1 Text Gate

**Status:** Completed.

**Files:**
- Modify: `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md`

**Acceptance:**
- Roadmap Phase 1 Gate says this repository owns text realtime orchestration only; ASR/TTS remain media service responsibilities.
- Gate commands include simulator basic/interrupt scenarios.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_agent_backend.py tests/test_realtime_event_mapping.py tests/test_realtime_backend_types.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_task_state.py tests/test_realtime_media_client_script.py tests/test_realtime_call_simulator.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario basic
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_realtime_call_simulator.py --scenario interrupt
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- AGENTS.md docs src tests scripts
```

## Scope Exclusions

- No ASR/TTS provider integration.
- No raw audio streaming, upload, persistence, or playback.
- No phone/IM platform integration.
- No Memory Intelligence v1.
- No Skill System v1.
- No multi-agent realtime fabric.
- No dashboard.
