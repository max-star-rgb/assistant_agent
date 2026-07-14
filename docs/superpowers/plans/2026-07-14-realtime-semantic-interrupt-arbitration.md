# Realtime Semantic Interrupt Arbitration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, bounded LLM control-plane arbiter that distinguishes realtime followups from implicit cancel/revise/replace utterances without starting a second same-session business runtime.

**Architecture:** Gateway continues to own queued-turn identity, admission, compare-and-cancel, and replacement serialization. A provider-neutral `RealtimeTurnArbiter` receives only the new utterance plus a prompt-safe realtime task-state snapshot; Gateway applies its validated decision only when `expected_run_id` still matches. Explicit interrupt remains deterministic and bypasses the arbiter, while mock/local/offline and every arbiter failure conservatively fall back to FIFO followup.

**Tech Stack:** Python 3.11, asyncio, Pydantic v2, existing `ChatAdapter`, Gateway in-memory queue/admission, pytest/unittest.

## Global Constraints

- Default runtime profile remains mock/local/offline and must not call a real Provider.
- Real LLM arbitration requires `provider_smoke` or `pilot`, a non-mock configured chat adapter, realtime entry capability, and explicit feature enablement.
- Explicit media/Gateway interrupt never waits for LLM arbitration.
- The arbiter never calls tools, memory, agent routing, or a second `AgentGraphRuntime`.
- A replacement backend run starts only after the cancelled backend exits and releases its permit.
- Low confidence, timeout, invalid output, Provider error, saturation, or stale `expected_run_id` must not cancel the active run.
- Runtime and lifecycle metadata must not contain utterance text, full prompts, raw Provider payloads, raw tool results, secrets, or long model explanations.
- Do not install dependencies or call real Providers during implementation or verification.

---

## File Map

**Create**

- `src/assistant_agent/schemas/realtime_turn_arbitration.py`: versioned request/decision contracts and deterministic normalization.
- `src/assistant_agent/services/realtime_turn_arbiter.py`: provider-neutral arbiter protocol, conservative fallback, ChatAdapter implementation, safe factory.
- `src/assistant_agent/gateway/turn_arbitration.py`: control-plane policy, bounded concurrency, timeout and slot-retention behavior.
- `tests/test_realtime_turn_arbiter.py`: contract, prompt, parsing, provider-safety and fallback tests.
- `tests/test_gateway_turn_arbitration.py`: Gateway disposition, serialization, race, cleanup and observability tests.

**Modify**

- `src/assistant_agent/gateway/queueing.py`: arbitration state on `QueuedTurn`.
- `src/assistant_agent/gateway/session.py`: eligibility, background arbitration lifecycle, compare-and-apply and no-backend terminal turns.
- `src/assistant_agent/gateway/capabilities.py`: trusted `supports_semantic_interrupt` capability.
- `src/assistant_agent/gateway/__init__.py`: public control-plane exports.
- `src/assistant_agent/api/gateway_runtime.py`: strict environment parsing and lazy arbiter/controller assembly.
- `src/assistant_agent/api/gateway_websocket.py`: trusted media session opt-out field.
- `src/assistant_agent/services/realtime_task_state.py`: structured revision semantics and cancel-only state transition.
- `tests/test_gateway_api.py`: environment, capability and media config contract tests.
- `tests/test_agent_service_websocket.py`: exact capability dictionary regression updates.
- `tests/test_realtime_task_state.py`: revision mapping and cancel-only state tests.
- `docs/gateway-architecture.md`: canonical runtime behavior, configuration and non-goals.

---

### Task 1: Versioned Arbitration Contracts and LLM Adapter

**Files:**

- Create: `src/assistant_agent/schemas/realtime_turn_arbitration.py`
- Create: `src/assistant_agent/services/realtime_turn_arbiter.py`
- Create: `tests/test_realtime_turn_arbiter.py`

**Interfaces:**

- Produces: `RealtimeTurnArbitrationRequest`, `RealtimeTurnArbitrationDecision`, `RealtimeTurnDisposition`, `RealtimeTurnRevisionType`, `normalize_arbitration_decision()`.
- Produces: async `RealtimeTurnArbiter.arbitrate(request)`, `ConservativeRealtimeTurnArbiter`, `ChatAdapterRealtimeTurnArbiter`, `create_realtime_turn_arbiter(config, chat_adapter)`.
- Consumes: existing `ProviderConfig`, `ChatAdapter`, `ChatRequest`, `ChatResult`, and prompt-safe `RealtimeTaskStateSnapshot.model_dump()` payloads.

- [ ] **Step 1: Write failing schema normalization tests**

Add tests that prove low confidence and invalid disposition combinations become `UNCERTAIN`, while trusted identity is copied from the request rather than accepted from model output:

```python
def test_normalize_arbitration_decision_rebinds_identity_and_rejects_low_confidence() -> None:
    request = RealtimeTurnArbitrationRequest(
        decision_id="d1",
        user_id="u1",
        session_id="s1",
        turn_id="t2",
        run_id="r2",
        expected_run_id="r1",
        utterance="改成上海",
        task_state={"objective": "查询北京天气"},
    )

    decision = normalize_arbitration_decision(
        {
            "disposition": "REVISE_ACTIVE",
            "revision_type": "replace_constraint",
            "confidence": 0.50,
            "reason_code": "corrects_constraint",
            "expected_run_id": "attacker-run",
        },
        request=request,
        min_confidence=0.80,
        source="semantic_llm",
    )

    assert decision.disposition == "UNCERTAIN"
    assert decision.expected_run_id == "r1"
    assert decision.decision_id == "d1"
    assert decision.fallback_reason == "low_confidence"
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_turn_arbiter.py -q
```

Expected: collection fails because the arbitration schema and service modules do not exist.

- [ ] **Step 3: Implement the Pydantic contracts and normalization**

Create these exact public shapes:

```python
REALTIME_TURN_ARBITRATION_SCHEMA_VERSION = "realtime_turn_arbitration_v1"
REALTIME_TURN_ARBITRATION_METADATA_KEY = "realtime_turn_arbitration"

RealtimeTurnDisposition = Literal[
    "FOLLOWUP",
    "CANCEL_ONLY",
    "REVISE_ACTIVE",
    "REPLACE_ACTIVE",
    "ACK_NOOP",
    "UNCERTAIN",
]
RealtimeTurnArbitrationSource = Literal[
    "semantic_llm",
    "deterministic_fallback",
]
RealtimeTurnRevisionType = Literal[
    "add_constraint",
    "replace_constraint",
    "change_goal",
    "cancel_goal",
    "confirm",
    "clarify",
]

class RealtimeTurnArbitrationRequest(BaseModel):
    schema_version: str = REALTIME_TURN_ARBITRATION_SCHEMA_VERSION
    decision_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    expected_run_id: str = Field(min_length=1, max_length=256)
    utterance: str = Field(min_length=1, max_length=1200)
    language: str | None = Field(default=None, max_length=32)
    task_state: dict[str, Any] = Field(default_factory=dict)

class RealtimeTurnArbitrationDecision(BaseModel):
    schema_version: str = REALTIME_TURN_ARBITRATION_SCHEMA_VERSION
    decision_id: str = Field(min_length=1, max_length=128)
    source: RealtimeTurnArbitrationSource
    disposition: RealtimeTurnDisposition
    revision_type: RealtimeTurnRevisionType | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=96)
    expected_run_id: str = Field(min_length=1, max_length=256)
    latency_ms: int = Field(default=0, ge=0)
    fallback_reason: str | None = Field(default=None, max_length=96)
```

`normalize_arbitration_decision()` must enforce the disposition/revision matrix from the design, clip `reason_code` to a lowercase machine token, overwrite `decision_id` and `expected_run_id` from the trusted request, and return `UNCERTAIN` for every validation failure.

- [ ] **Step 4: Add failing ChatAdapter arbiter tests**

Cover valid JSON, fenced JSON, Provider error, malformed JSON, no tools, bounded prompt, and profile safety. The valid case must assert:

```python
decision = asyncio.run(arbiter.arbitrate(request))
assert decision.disposition == "REVISE_ACTIVE"
assert adapter.requests[0].tools == []
assert adapter.requests[0].tool_choice is None
assert adapter.requests[0].response_format == {"type": "json_object"}
assert adapter.requests[0].temperature == 0.0
assert adapter.requests[0].max_tokens == 256
assert "raw tool" not in adapter.requests[0].user_query
```

- [ ] **Step 5: Implement the provider-neutral arbiter service**

Implement:

```python
class RealtimeTurnArbiter(Protocol):
    async def arbitrate(
        self,
        request: RealtimeTurnArbitrationRequest,
    ) -> RealtimeTurnArbitrationDecision:
        """Return one structured control-plane decision."""

class ConservativeRealtimeTurnArbiter:
    async def arbitrate(self, request: RealtimeTurnArbitrationRequest) -> RealtimeTurnArbitrationDecision:
        return uncertain_arbitration_decision(
            request,
            source="deterministic_fallback",
            fallback_reason="llm_arbiter_disabled",
        )

class ChatAdapterRealtimeTurnArbiter:
    def __init__(self, chat_adapter: ChatAdapter, *, min_confidence: float) -> None:
        self.chat_adapter = chat_adapter
        self.min_confidence = min_confidence

    async def arbitrate(self, request: RealtimeTurnArbitrationRequest) -> RealtimeTurnArbitrationDecision:
        started = time.perf_counter()
        result = await asyncio.to_thread(
            self.chat_adapter.chat,
            ChatRequest(
                user_id=request.user_id,
                session_id=request.session_id,
                user_query=_arbitration_prompt(request),
                system_instruction=_ARBITRATION_SYSTEM_INSTRUCTION,
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=256,
            ),
        )
        if not result.success:
            return uncertain_arbitration_decision(
                request,
                fallback_reason="provider_error",
                latency_ms=_elapsed_ms(started),
            )
        return normalize_arbitration_decision(
            _extract_json_object(result.response_text),
            request=request,
            min_confidence=self.min_confidence,
            source="semantic_llm",
            latency_ms=_elapsed_ms(started),
        )
```

The factory signature is `create_realtime_turn_arbiter(config, chat_adapter, *, min_confidence=0.80)`. It returns the conservative implementation unless the profile is `provider_smoke|pilot` and adapter provider is neither `mock` nor missing/unconfigured.

- [ ] **Step 6: Run contract and service tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_turn_arbiter.py -q
```

Expected: all tests pass and no network is used.

- [ ] **Step 7: Commit the contract slice**

```bash
git add src/assistant_agent/schemas/realtime_turn_arbitration.py src/assistant_agent/services/realtime_turn_arbiter.py tests/test_realtime_turn_arbiter.py
git commit -m "feat: add realtime turn arbitration contracts"
```

---

### Task 2: Bounded Control-Plane Arbitration Controller

**Files:**

- Create: `src/assistant_agent/gateway/turn_arbitration.py`
- Modify: `src/assistant_agent/gateway/__init__.py`
- Test: `tests/test_realtime_turn_arbiter.py`

**Interfaces:**

- Consumes: `RealtimeTurnArbiter` and `RealtimeTurnArbitrationRequest` from Task 1.
- Produces: `GatewayTurnArbitrationPolicy`, `GatewayTurnArbitrationController.decide(request)` and prompt-safe `GatewayTurnArbitrationOutcome`.

- [ ] **Step 1: Write failing timeout, saturation and slot-retention tests**

Use a blocking arbiter whose underlying task remains alive after caller timeout. Assert that a second decision gets `control_plane_saturated` until the first real call finishes, rather than releasing the slot on timeout:

```python
first = await controller.decide(request_one)
second = await controller.decide(request_two)

assert first.decision.fallback_reason == "arbitration_timeout"
assert second.decision.fallback_reason == "control_plane_saturated"
blocking_arbiter.release.set()
await blocking_arbiter.finished.wait()
third = await controller.decide(request_three)
assert third.decision.disposition == "FOLLOWUP"
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_turn_arbiter.py -q
```

Expected: imports fail for `assistant_agent.gateway.turn_arbitration`.

- [ ] **Step 3: Implement validated policy and bounded controller**

Implement the exact policy defaults:

```python
@dataclass(frozen=True)
class GatewayTurnArbitrationPolicy:
    enabled: bool = False
    timeout_ms: int = 1000
    max_concurrency: int = 2
    min_confidence: float = 0.80

    def __post_init__(self) -> None:
        if isinstance(self.timeout_ms, bool) or self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        if isinstance(self.max_concurrency, bool) or self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        if not math.isfinite(self.min_confidence) or not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be finite and between 0 and 1")
```

`decide()` must acquire a slot without waiting, start one background arbiter task, use `asyncio.wait_for(asyncio.shield(task), timeout)`, and retain the slot until that task truly finishes. Timeout/caller cancellation adds a done callback that releases the slot; success releases it immediately. Every controller-generated fallback uses the Task 1 decision contract. Before returning any successful injected/default arbiter result, call `normalize_arbitration_decision()` again with `policy.min_confidence`; the controller is the authoritative threshold gate even when tests or future adapters inject a decision directly.

- [ ] **Step 4: Export only the deliberate Gateway public interfaces**

Add `GatewayTurnArbitrationController` and `GatewayTurnArbitrationPolicy` to `assistant_agent.gateway.__all__`; keep internal slot/task helpers private.

- [ ] **Step 5: Run arbiter/controller tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_turn_arbiter.py -q
```

Expected: all tests pass, including a proof that timed-out synchronous calls retain their bounded slot.

- [ ] **Step 6: Commit the controller slice**

```bash
git add src/assistant_agent/gateway/turn_arbitration.py src/assistant_agent/gateway/__init__.py tests/test_realtime_turn_arbiter.py
git commit -m "feat: bound semantic interrupt arbitration"
```

---

### Task 3: Realtime Task-State Revision Semantics

**Files:**

- Modify: `src/assistant_agent/services/realtime_task_state.py`
- Modify: `tests/test_realtime_task_state.py`

**Interfaces:**

- Consumes: normalized `realtime_turn_arbitration` metadata from Task 1.
- Produces: `apply_cancel_only_arbitration_to_task_state()` for the Gateway control-only path.
- Preserves: `prepare_realtime_task_state_request()` and existing artifact/side-effect snapshot contracts.

- [ ] **Step 1: Write failing revision behavior tests**

Add exact cases for `replace_constraint`, `change_goal`, `confirm`, and cancel-only. The change-goal assertion must prove side effects survive while old artifacts become stale:

```python
assert updated.objective == "改为设置明天提醒"
assert updated.constraints == []
assert updated.revisions[-1].revision_type == "change_goal"
assert all(artifact.reuse_policy == "stale" for artifact in updated.artifacts)
assert updated.side_effects == original.side_effects
```

Cancel-only must assert:

```python
cancelled = apply_cancel_only_arbitration_to_task_state(
    user_id="u1",
    session_id="s1",
    turn_id="t2",
    run_id="r2",
    user_text="先别查了",
    decision=decision,
    store=store,
)
assert cancelled.status == "cancelled"
assert cancelled.revisions[-1].revision_type == "cancel_goal"
assert cancelled.side_effects == original.side_effects
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_task_state.py -q
```

Expected: new assertions fail because every interrupt currently becomes `add_constraint`.

- [ ] **Step 3: Implement normalized revision application**

Read only the validated metadata key. Apply the design matrix:

```python
if revision_type == "add_constraint":
    state.constraints = _append_unique_limited(state.constraints, text)
elif revision_type == "replace_constraint":
    state.constraints = [text] if text else []
elif revision_type == "change_goal":
    state.objective = text
    state.constraints = []
    state.artifacts = [_stale_artifact(artifact) for artifact in state.artifacts]
elif revision_type in {"confirm", "clarify"}:
    pass
```

Append one `IntentRevision` with `metadata.source=semantic_llm` and the decision id. Keep the current deterministic continuation strategy and side-effect records.

- [ ] **Step 4: Implement the narrow cancel-only service function**

The function loads a copied state, sets `status="cancelled"`, `tts_state="interrupted"`, records a `cancel_goal` revision, preserves artifacts and side effects, saves through `RealtimeTaskStateStore`, and returns a copied state. It must not expose store internals to Gateway.

- [ ] **Step 5: Run task-state tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_task_state.py tests/test_tool_result_presentation.py -q
```

Expected: all tests pass and existing explicit interrupt behavior remains `add_constraint`.

- [ ] **Step 6: Commit the task-state slice**

```bash
git add src/assistant_agent/services/realtime_task_state.py tests/test_realtime_task_state.py
git commit -m "feat: apply semantic interrupt task revisions"
```

---

### Task 4: Gateway Background Arbitration and Compare-and-Apply

**Files:**

- Modify: `src/assistant_agent/gateway/queueing.py`
- Modify: `src/assistant_agent/gateway/session.py`
- Create: `tests/test_gateway_turn_arbitration.py`

**Interfaces:**

- Consumes: `GatewayTurnArbitrationController`, request/decision contracts, and Task 3 cancel-only service.
- Produces: same Gateway wire lifecycle plus prompt-safe arbitration fields; no new business runtime interface.
- Preserves: existing explicit `_message_requests_interrupt()` behavior and `expected_run_id` cancellation guard.

- [ ] **Step 1: Write failing eligibility and explicit-bypass tests**

Prove:

- no active run invokes no arbiter;
- active run plus explicit interrupt invokes no arbiter and cancels immediately;
- active run plus trusted capability starts arbiter in a background task while the first backend remains active;
- ordinary `/ws/gateway`-style capability false stays FIFO without invoking arbiter.

The background test must assert that the receive loop can process a `ping` and return `pong` while arbitration is blocked.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_arbitration.py -q
```

Expected: service construction fails because it does not accept an arbitration controller.

- [ ] **Step 3: Add queued-turn arbitration lifecycle fields**

Extend `QueuedTurn` with:

```python
arbitration_pending: bool = False
arbitration_decision_id: str | None = None
arbitration_expected_run_id: str | None = None
arbitration_task: asyncio.Task[None] | None = None
```

Cancellation, queue timeout and session close must cancel the orchestration task. The bounded controller remains responsible for retaining Provider slots when an underlying call cannot be hard-cancelled.

- [ ] **Step 4: Add controller injection and background scheduling**

Add optional `turn_arbitration_controller` to `GatewaySessionService` and `GatewaySessionManager`, sharing one controller across managed sessions. In `_handle_user_message()`:

```python
explicit_interrupt = _message_requests_interrupt(payload, self._config)
eligible_active = self._active_by_session.get(session_id)
semantic_candidate = (
    not explicit_interrupt
    and eligible_active is not None
    and self._semantic_interrupt_enabled(payload)
)
```

Accept/reserve the turn exactly once. For a semantic candidate, mark it pending, keep its FIFO position, emit normal `run.queued`, schedule `_arbitrate_turn(turn, expected_run_id)`, and return without awaiting LLM.

- [ ] **Step 5: Prevent promotion while arbitration is pending**

Centralize pending promotion in `_promote_next_locked(session_id) -> QueuedTurn | None`. It may set an arbitration-pending turn as the session current head, but returns `None` until that turn is resolved. When arbitration later resolves to followup and the old run has ended, `_apply_arbitration_decision()` schedules that current turn exactly once.

- [ ] **Step 6: Write failing disposition tests**

Add scripted decisions and assert:

- `FOLLOWUP`: old cancel token remains false and new backend starts after old completion.
- `REVISE_ACTIVE`: new turn moves to the front, old token is cancelled, old backend exits before new starts, metadata has `control=interrupt` and `revision_type`.
- `REPLACE_ACTIVE`: same serialization with `revision_type=change_goal`.
- `UNCERTAIN`: same behavior as followup, with lifecycle fallback metadata.

- [ ] **Step 7: Implement compare-and-apply under the Gateway lock**

The locked section validates turn state, decision id and active `expected_run_id`. It returns an action object rather than awaiting endpoint, task-state or Provider work while holding the lock. The action variants are:

```python
Literal[
    "followup",
    "cancel_only",
    "revise_active",
    "replace_active",
    "ack_noop",
    "stale_followup",
    "discard",
]
```

For revise/replace, remove the turn from its current deque position, `appendleft`, set `runtime_interrupt=True`, and attach the prompt-safe decision metadata. Cancel only the active run whose id equals `expected_run_id`.

- [ ] **Step 8: Run disposition and existing queue serialization tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_arbitration.py tests/test_gateway_session.py -q
```

Expected: all tests pass; existing explicit interrupt still waits for old backend release.

- [ ] **Step 9: Commit the Gateway arbitration slice**

```bash
git add src/assistant_agent/gateway/queueing.py src/assistant_agent/gateway/session.py tests/test_gateway_turn_arbitration.py
git commit -m "feat: arbitrate implicit realtime interrupts"
```

---

### Task 5: Control-Only Turns, Races, Cleanup and Observability

**Files:**

- Modify: `src/assistant_agent/gateway/session.py`
- Modify: `tests/test_gateway_turn_arbitration.py`

**Interfaces:**

- Consumes: Task 4 compare-and-apply action and Task 3 cancel-only state service.
- Produces: no-backend `run.end(reason=completed)` for `CANCEL_ONLY|ACK_NOOP` and bounded lifecycle events.

- [ ] **Step 1: Write failing control-only terminal tests**

For `CANCEL_ONLY`, assert the old run ends cancelled, the new control turn ends completed with no `run.started`, no second backend request exists, reservation is released, and task-state status is cancelled. For `ACK_NOOP`, assert old run continues and the new turn ends completed without backend.

Required payload assertions:

```python
assert control_end["reason"] == "completed"
assert control_end["payload"]["handled_by"] == "turn_arbiter"
assert control_end["payload"]["expects_reply"] is False
assert control_end["payload"]["arbitration"]["disposition"] == "CANCEL_ONLY"
assert "utterance" not in str(control_end["payload"])
```

- [ ] **Step 2: Implement `_complete_arbitrated_control_turn()`**

Remove the turn from current/pending structures, set dedupe terminal state, cancel its queue timeout/orchestration task, release ticket/reservation, emit one completed `run.end`, and schedule only a ready promoted turn. Do not call `_cancel_queued_turn()`, because the new control turn is handled successfully rather than cancelled.

- [ ] **Step 3: Write failing stale-decision and multi-interruption tests**

Cover:

- old run finishes before decision;
- two implicit utterances target the same old run;
- queued `run.cancel` happens during arbitration;
- queue timeout happens during arbitration;
- session close/hangup happens during arbitration.

Every late decision must avoid cancelling the replacement/current run. A still-accepted stale turn becomes FIFO followup; a terminal turn is discarded.

- [ ] **Step 4: Implement cleanup and stale outcome handling**

Use `decision_id + expected_run_id + turn.state` checks. Add one helper that cancels only the orchestration task and never directly releases the controller’s Provider slot. Ensure `_consume_background_task()` absorbs expected cancellation without hiding lifecycle fallback events.

- [ ] **Step 5: Add prompt-safe lifecycle events**

Emit only:

```python
{
    "decision_id": decision.decision_id,
    "source": decision.source,
    "disposition": decision.disposition,
    "normalized_disposition": normalized_disposition,
    "confidence_bucket": _confidence_bucket(decision.confidence),
    "reason_code": decision.reason_code,
    "latency_ms": decision.latency_ms,
    "fallback_reason": decision.fallback_reason,
    "expected_run_matched": expected_run_matched,
}
```

Events are `gateway.turn.arbitration.started|finished|fallback|stale`; no user text or task-state payload is allowed.

- [ ] **Step 6: Run the full arbitration and cancellation slice**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_turn_arbitration.py tests/test_gateway_session.py tests/test_realtime_turn_cancellation.py tests/test_phase1_realtime_loop_deep_gate.py -q
```

Expected: all tests pass and stale old-run output remains suppressed after apply.

- [ ] **Step 7: Commit the race-hardening slice**

```bash
git add src/assistant_agent/gateway/session.py tests/test_gateway_turn_arbitration.py
git commit -m "test: harden semantic interrupt lifecycle races"
```

---

### Task 6: Trusted Capability and Runtime Configuration Wiring

**Files:**

- Modify: `src/assistant_agent/gateway/capabilities.py`
- Modify: `src/assistant_agent/api/gateway_runtime.py`
- Modify: `src/assistant_agent/api/gateway_websocket.py`
- Modify: `tests/test_gateway_api.py`
- Modify: `tests/test_agent_service_websocket.py`

**Interfaces:**

- Consumes: Task 1 arbiter factory and Task 2 controller/policy.
- Produces: one process-shared controller assembled lazily by `create_gateway_session_manager()`.
- Preserves: product entry layers do not import or construct `AgentGraphRuntime` directly.

- [ ] **Step 1: Write failing capability and configuration tests**

Assert realtime media capability is true while generic Gateway and agent-service capabilities are false. Assert exact env parsing:

```python
assert manager.turn_arbitration_controller.policy == GatewayTurnArbitrationPolicy(
    enabled=True,
    timeout_ms=750,
    max_concurrency=3,
    min_confidence=0.85,
)
```

Reject zero/negative timeout, zero concurrency, non-finite confidence and confidence outside `[0, 1]`.

- [ ] **Step 2: Add the trusted capability and media opt-out field**

Add `supports_semantic_interrupt: bool = False` to `EntryAdapterCapabilities` and `to_metadata()`. Set it true only on `REALTIME_MEDIA_ENTRY_CAPABILITIES`. Add `semantic_interrupt_enabled` to `MEDIA_CONFIG_BOOL_KEYS`, allowing a trusted session to opt out while absent inherits the global controller setting.

- [ ] **Step 3: Add strict environment constants and parsing**

Add:

```python
REALTIME_SEMANTIC_INTERRUPT_ENABLED_ENV = "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_ENABLED"
REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS_ENV = "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_TIMEOUT_MS"
REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY_ENV = "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MAX_CONCURRENCY"
REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE_ENV = "MULTIMODAL_AGENT_REALTIME_SEMANTIC_INTERRUPT_MIN_CONFIDENCE"
```

Build a `GatewayTurnArbitrationPolicy` with defaults `false/1000/2/0.80`. Add a finite unit-interval parser for confidence.

- [ ] **Step 4: Assemble the arbiter lazily through AssistantRuntimeApp**

Use a local import inside the default factory:

```python
def _default_realtime_turn_arbiter(*, min_confidence: float) -> RealtimeTurnArbiter:
    from assistant_agent.api.routes_agent import get_assistant_runtime_app

    runtime = get_assistant_runtime_app().runtime
    return create_realtime_turn_arbiter(
        runtime.config,
        runtime.chat_adapter,
        min_confidence=min_confidence,
    )
```

Pass `lambda: _default_realtime_turn_arbiter(min_confidence=policy.min_confidence)` as the shared controller's lazy factory. Do not import `AgentGraphRuntime` in `gateway_runtime.py`, and do not instantiate a real adapter merely because an API key exists.

- [ ] **Step 5: Update exact capability dictionary assertions**

Add `supports_semantic_interrupt` to exact expected dictionaries in `tests/test_gateway_api.py` and `tests/test_agent_service_websocket.py`; expected values must match the three entry profiles.

- [ ] **Step 6: Run runtime/config/architecture tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_gateway_api.py tests/test_agent_service_websocket.py tests/test_architecture_boundaries.py tests/test_runtime_profile_safety.py -q
```

Expected: all tests pass without real Provider calls.

- [ ] **Step 7: Commit the wiring slice**

```bash
git add src/assistant_agent/gateway/capabilities.py src/assistant_agent/api/gateway_runtime.py src/assistant_agent/api/gateway_websocket.py tests/test_gateway_api.py tests/test_agent_service_websocket.py
git commit -m "feat: wire opt-in semantic interrupt arbitration"
```

---

### Task 7: Canonical Documentation and Final Verification

**Files:**

- Modify: `docs/gateway-architecture.md`
- Retain: `docs/superpowers/specs/2026-07-14-realtime-semantic-interrupt-arbitration-design.md`
- Retain: `docs/superpowers/plans/2026-07-14-realtime-semantic-interrupt-arbitration.md`

**Interfaces:**

- Documents the implemented Gateway/control/runtime boundary and operator configuration.
- Does not turn the retained spec or plan into the canonical architecture source.

- [ ] **Step 1: Update the canonical Gateway architecture**

Add a “Realtime semantic interrupt arbitration” section containing:

- explicit control bypass;
- trusted realtime-only eligibility;
- six dispositions and `UNCERTAIN -> FOLLOWUP` fallback;
- independent bounded control-plane concurrency;
- `expected_run_id` compare-and-apply;
- no same-session backend overlap;
- task-state revision/side-effect rules;
- Media Relay audio-duck responsibility;
- four environment variables and defaults;
- default offline/provider safety;
- live steer as a non-goal.

Update the document date only if content changes are successfully implemented.

- [ ] **Step 2: Run the targeted regression suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_realtime_turn_arbiter.py tests/test_gateway_turn_arbitration.py tests/test_gateway_session.py tests/test_gateway_api.py tests/test_realtime_task_state.py tests/test_realtime_turn_cancellation.py tests/test_phase1_realtime_loop_deep_gate.py tests/test_architecture_boundaries.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the repository fast suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: all fast tests pass; no real Provider is invoked.

- [ ] **Step 4: Run environment and static repository checks**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
git diff --check
git status --short
```

Expected: environment check succeeds, `git diff --check` emits no errors, and status lists only this feature plus pre-existing unrelated user files.

- [ ] **Step 5: Review runtime safety evidence**

Confirm from test output and diff that:

- no real Provider call occurred;
- explicit interrupt tests never invoke the arbiter;
- all failure paths are non-cancelling followups;
- no control-plane payload contains utterance or raw context;
- replacement backend concurrency remains one per session;
- no unrelated untracked `improvement-lab` files were staged or modified.

- [ ] **Step 6: Commit documentation and final integration**

```bash
git add docs/gateway-architecture.md docs/superpowers/specs/2026-07-14-realtime-semantic-interrupt-arbitration-design.md docs/superpowers/plans/2026-07-14-realtime-semantic-interrupt-arbitration.md
git commit -m "docs: document realtime semantic interrupt arbitration"
```

Do not stage `docs/superpowers/specs/2026-07-14-improvement-lab-design.md` or `docs/superpowers/plans/2026-07-14-improvement-lab.md`; they are unrelated user-owned files.

## Execution record (2026-07-14)

Implementation followed the slices above and retained the feature on the isolated
`feature/realtime-semantic-interrupt` worktree branch. Completion evidence:

- targeted semantic-interrupt/Gateway/task-state/cancellation/architecture suite:
  `129 passed`;
- repository fast suite: `178 passed, 1792 deselected`;
- environment check: Python 3.12.13, no missing imports or paths, `ok=true`;
- `git diff --check`: clean;
- no real Provider was invoked.

The planned targeted command included
`tests/test_phase1_realtime_loop_deep_gate.py`. That file currently has two stale
assertions: it rejects the already-established `run.queued` frame and expects an
older cancellation payload. A read-only comparison produced the same `2 failed,
2 passed` result on both this feature worktree and untouched `cqy`, so those
unrelated assertions were not rewritten in this feature. An earlier full-suite
diagnostic also exposed a timing-sensitive agent-service WebSocket shutdown
failure and reproduced it on untouched `cqy`; it remains separate baseline debt.

Final self-review additionally hardened trusted entry eligibility, the decision
matrix, bounded prompt projection, huge-count handling, and cancel-reason
observability before the completion suites above were rerun.
