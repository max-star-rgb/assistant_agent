# Agent Service Async Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep `/agent-service/v1` responsive to media while long chat turns run, and add application-level response delivery acknowledgment with prompt-safe audit evidence.

**Architecture:** A connection runtime owns tracked chat tasks and a serialized outbound sender. A focused delivery service owns correlation ids, state transitions, and JSONL audit; optional client capabilities enable progress and ACK without changing legacy behavior.

**Tech Stack:** Python 3.12, asyncio, FastAPI WebSocket, existing Gateway facade, Pydantic/dataclasses, JSONL, pytest.

## Global Constraints

- Preserve legacy clients: no progress frames unless negotiated.
- Never log or audit response text, raw media, phone numbers, credentials, or provider payloads.
- `sent` means `send_text()` returned; only `acked` means media application confirmation.
- Gateway remains authoritative for run lifecycle and same-session ordering.
- Do not modify unavailable media-service sources or add dependencies.

---

### Task 1: Delivery Registry And Audit

**Files:**
- Create: `src/assistant_agent/services/agent_service_delivery.py`
- Create: `tests/test_agent_service_delivery.py`

**Interfaces:**
- `AgentServiceDeliveryRegistry(audit_sink)`
- `accept(session_id, chat_index, expects_ack) -> AgentServiceDelivery`
- `mark_processing()`, `mark_sent(run_id, trace_id)`, `ack()`, `mark_failed()`, `mark_disconnected()`
- `JsonlAgentServiceDeliveryAudit(path)` writes redacted `agent_service_delivery_v1` records.

- [ ] Write tests proving accepted -> sent -> acked, unknown/duplicate/mismatch ACK rejection, disconnect-before-send/ack transitions, and redaction.
- [ ] Run `pytest tests/test_agent_service_delivery.py -q`; expect import failure.
- [ ] Implement a lock-protected registry, opaque UUID delivery ids, SHA-256 digests for session/chat identifiers, and JSONL audit append.
- [ ] Run the focused test; expect PASS.
- [ ] Commit with `git commit -m "Add agent service delivery audit"`.

### Task 2: Concurrent Chat Runtime

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/test_agent_service_websocket.py`

**Interfaces:**
- Connection state gains `send_lock`, `chat_tasks`, `closed`, and delivery registry.
- `_send_response()` accepts state and serializes sends.
- Chat frames are accepted into tracked tasks; non-chat frames remain inline.

- [ ] Add a failing test with a blocked Gateway facade: send chat, then video, assert `videoResponse` arrives before releasing chat and final `chatResponse` arrives after release.
- [ ] Add a concurrent-send probe test asserting maximum active `send_text` calls equals one.
- [ ] Run both tests and confirm they fail because the receive loop awaits chat.
- [ ] Refactor the route to parse chat, schedule `_run_chat_and_send()`, and immediately resume receive. Keep parsing/validation errors synchronous.
- [ ] On disconnect mark state closed, cancel/gather chat tasks with bounded grace, close Gateway, then clean video context.
- [ ] Run all agent-service tests and adjacent Gateway tests; expect PASS.
- [ ] Commit with `git commit -m "Keep agent service media responsive during chat"`.

### Task 3: Negotiated Progress And Response ACK

**Files:**
- Modify: `src/assistant_agent/api/agent_service_websocket.py`
- Modify: `tests/test_agent_service_websocket.py`
- Modify: `src/assistant_agent/gateway/capabilities.py` only if prompt-safe capability metadata needs an explicit delivery field; otherwise keep transport negotiation out of the prompt.

**Interfaces:**
- `assistantControl.clientCapabilities.chatProgress/chatResponseAck` are optional booleans.
- New inbound/outbound type `chatResponseAck`.
- Optional outbound `chatProgress` with `chatIndex`, `deliveryId`, `status=PROCESSING`.
- Final media-style chat body gains `deliveryId`.

- [ ] Write failing legacy test proving no progress is emitted without negotiation.
- [ ] Write failing negotiated test proving immediate progress, periodic progress via short injected interval, final delivery id, ACK success, duplicate failure, and audit `acked`.
- [ ] Implement capability parsing, progress task lifecycle, final correlation, ACK handler, and delivery state transitions.
- [ ] Ensure a send failure becomes `failed` or `disconnected_before_send` without a second send attempt.
- [ ] Run `pytest tests/test_agent_service_websocket.py tests/test_agent_service_delivery.py -q`; expect PASS.
- [ ] Commit with `git commit -m "Add acknowledged agent service chat delivery"`.

### Task 4: Documentation And Verification

**Files:**
- Modify: `docs/media-agent-service-websocket.md`
- Modify: `docs/gateway-architecture.md`
- Modify: `docs/observability-harness.md`

- [ ] Document concurrency, capability negotiation, progress cadence, ACK semantics, 90-second media timeout requirement, close evidence, and audit retention/redaction.
- [ ] Run focused tests covering delivery, WebSocket, H.264 ingestion, native handoff, Gateway session, and architecture boundaries.
- [ ] Run `scripts/check_env.py` and `pytest -m fast -q`.
- [ ] Run full pytest; compare any failures against `cqy` baseline before attributing them.
- [ ] Run a local delayed fake-facade WebSocket smoke proving video ACK during chat and an ACKed final response without real providers.
- [ ] Commit docs with `git commit -m "Document async media response delivery"`.
- [ ] Review `git diff --check`, branch log, status, and retained user files before integration.
