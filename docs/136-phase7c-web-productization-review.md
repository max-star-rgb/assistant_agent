# 136 Phase 7C Web Productization Review

## Scope Completed

Phase 7C upgraded the existing simple Web Console into a minimal usable pilot surface while preserving the default mock/local/offline runtime. After user review, the page was adjusted from a form-style result viewer into a chat-first assistant entry that mirrors `scripts/demo_assistant_loop.py`.

Completed items:

- Scenario picker improvements with client-side filtering.
- Browser-session request history using `sessionStorage`.
- Runtime profile and provider mode indicator backed by `/demo/runtime-info`.
- Run detail panel using `/runs/{run_id}`.
- Trace detail panel using `/traces/{trace_id}`.
- Tool-call timeline derived from API run payloads.
- Chat transcript with user / assistant messages.
- ReAct process panel showing thought / action / observation / final-answer details from `react_steps`.
- Capability badges for direct chat and tool-based runs.
- Loading spinner, disabled run button, and visible error banner.
- Offline-safe API and page tests.

## Safety Review

Default behavior remains local/offline:

```text
runtime_profile: local_demo
chat provider: mock
vision provider: mock
product / price / image / render / video providers: mock or local
```

The Web Console does not collect or persist real user data in the repo. Browser history is session-local only. The runtime info endpoint exposes provider names and runtime mode only; it does not return API keys, authorization headers, bearer tokens, base URLs, or raw provider responses.

## API Surface

The Web Console uses these existing or new local endpoints:

```text
GET  /demo/scenarios
GET  /demo/runtime-info
POST /agent/run
GET  /runs/{run_id}
GET  /traces/{trace_id}
GET  /runs/{run_id}/tool-calls
```

`/demo/runtime-info` was added for a redacted UI mode indicator.

`POST /agent/run` now also includes a redacted `react_steps` list so the Web Console can explain the assistant loop without exposing API keys, authorization headers, bearer tokens, raw provider responses, or full media payloads.

## Test Coverage

Added coverage in:

```text
tests/test_phase7c_web_productization.py
```

Updated compatibility coverage in:

```text
tests/test_phase6b_web_console.py
```

Covered behaviors:

- Runtime info is redacted and offline by default.
- Console page contains chat transcript, ReAct process panel, scenario filtering, request history, detail panels, timeline, badges, loading, and error state markers.
- Run / trace / tool-call endpoints support the console flow.
- Agent run responses expose redacted `react_steps` for the Web Console.

## Known Limits

- The console remains a no-framework static HTML page.
- Browser-session history is not shared across browsers or restarts.
- Trace and run panels show JSON summaries rather than a fully designed inspector.
- Real Provider smoke remains manual and opt-in outside the default Web Console flow.

## Phase 7C Result

Phase 7C is complete. Do not proceed to Phase 7D unless explicitly requested.

## Inserted Tasks 135-1 / 135-2

After Web Console review, two inserted productization tasks were completed:

```text
Task 135-1 Shared Assistant Run Backend for CLI and Web
Task 135-2 Realtime Agent Progress Events for Web UI
```

The shared backend is:

```text
src/multimodal_agent/services/assistant_run_service.py
```

It is now the common run path for:

```text
scripts/demo_assistant_loop.py
POST /agent/run
WebSocket /ws/agent/{session_id}
```

The shared run payload exposes:

```text
response_text
react_steps
tool_calls
run_id
trace_id
runtime_info
current_stage
blocked_reason
errors
```

WebSocket progress now streams events while the graph runtime is running instead of only replaying them after completion. This lets a Web UI show where the agent is currently working or blocked.
