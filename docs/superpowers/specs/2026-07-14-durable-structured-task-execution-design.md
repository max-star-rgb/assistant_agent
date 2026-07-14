# Durable Structured Task Execution Design

Date: 2026-07-14

Status: Approved and self-reviewed; ready for implementation planning

## 1. Purpose

`assistant_agent` currently lets the provider-native LLM answer or call governed
tools directly. The repository retains `TaskPlan`, `PlanValidator`, and plan-mode
state, but `execution_strategy=plan_and_solve` is primarily a compatibility hint
in the real provider-native path and does not reliably create a durable,
structured task before execution.

This design adds an optional slow path for complex or explicitly asynchronous
work. Simple requests keep the existing low-latency ReAct behavior. Complex work
can be converted into a validated, user-visible, durable task that survives the
original request, process restarts, and client disconnects.

The design preserves these repository boundaries:

- `AgentGraphRuntime` and the assistant loop remain the single agent brain.
- The LLM decides whether an `auto` request needs the slow path; no keyword or
  intent classifier takes over real-provider tool selection.
- Business tool execution still passes through `ActionValidator`,
  `ToolExecutor`, `ToolRegistry`, policy, budget, and audit boundaries.
- Gateway owns entry/session/run/cancel and transport semantics, but does not
  become the durable scheduler or task store.
- Task context remains distinct from conversation history, long-term memory,
  tool observations, and raw provider payloads.

## 2. Goals and Non-goals

### Goals

- Support hybrid activation: callers or users may explicitly require durable
  execution, while the LLM may autonomously select it for complex `auto` tasks.
- Represent work as a bounded DAG with validated dependencies and immutable plan
  revisions.
- Return a stable task identifier quickly instead of keeping an ingress request
  alive for long-running work.
- Persist task state, step progress, approvals, artifacts, and replayable events.
- Resume safely after worker failure or process restart.
- Apply risk-based confirmation at execution time.
- Expose plans and progress without exposing model chain-of-thought.
- Support dynamic replanning within local step, revision, cost, and time limits.

### Non-goals for the first version

- A second planner LLM or a separate planner/controller loop.
- A general workflow DSL with arbitrary conditions, loops, or compensation.
- PostgreSQL, distributed scheduling, or cross-region execution.
- General exactly-once delivery or automatic rollback of arbitrary side effects.
- Automatic promotion of completed task state into long-term memory.
- Replacing the current Gateway admission queue or assistant loop.

## 3. Selected Architecture

The selected approach is a single LLM loop plus a durable task execution layer.
The provider-native assistant receives an internal, governed
`task_plan_submit` capability. It can select this capability instead of directly
calling a business tool when the request requires durable execution.

```text
ordinary request --------------------> current ReAct loop
                                            |
complex or explicitly durable request       | task_plan_submit
                                            v
                                      PlanValidator
                                            |
                                            v
                                    DurableTaskService
                                            |
                                  lease + short execution quantum
                                            |
                                            v
                                    same assistant loop
                                            |
                                            v
                          ActionValidator -> ToolExecutor -> ToolRegistry
```

`task_plan_submit` is a provider-native structured control capability, not an
independent planner. Its execution must use the existing governed tool path. It
may create a task only after schema, plan, identity, budget, and policy checks
succeed.

The model-facing input contains the proposed plan and a short revision reason;
it never accepts `user_id`, `session_id`, `task_id`, lease tokens, confirmation
state, or expected aggregate versions from the model. On an ingress run,
`ToolContext` has no durable-task binding and the tool creates a new task. On a
worker resume, trusted runtime context binds the current `task_id`, lease token,
and expected task/plan versions, so the same tool creates an immutable revision
of that task. The model cannot select or revise another task by supplying an ID.

## 4. Request Modes and Activation

Add a request-level `task_execution_mode` with three values:

- `auto` (default): the LLM may answer directly, call a business tool, or submit
  a durable task.
- `durable`: the LLM must submit a valid plan before any business tool executes.
- `foreground`: the LLM must stay in the current foreground ReAct path and may
  not create a durable task.

User language such as an explicit request to plan, run asynchronously, or finish
later remains semantic input to the LLM. It is not converted into a local
keyword router. Product/API callers that require deterministic behavior use the
structured request field.

For compatibility, when the feature is enabled,
`execution_strategy=plan_and_solve` maps to `task_execution_mode=durable` if the
new field is absent. When both fields are supplied, the explicit
`task_execution_mode` value wins. When neither is supplied, the mode is `auto`.
When the feature is disabled, an omitted new field preserves the legacy
`plan_and_solve` behavior, so the closed feature flag does not break existing
callers. `UserRequest` uses Pydantic's `model_fields_set` to distinguish an
omitted field from an explicitly supplied `auto`; a runtime normalization helper
combines that fact with configuration and returns a request carrying a concrete
effective mode.

In `durable` mode, a direct business tool call before task creation is rejected
locally with a recovery observation instructing the assistant to submit a plan.
The model cannot opt out of the caller's durable requirement.

When the feature flag is disabled, `auto` and `foreground` requests do not
expose `task_plan_submit`. An explicit `durable` request fails during runtime
preflight with `DURABLE_TASKS_DISABLED`, before calling the LLM. Reserved
runtime metadata for task binding, leases, confirmations, and idempotency is
removed from public request metadata and can only be injected by trusted local
services.

## 5. Domain Model

### 5.1 TaskRecord

The stable task aggregate contains:

- `task_id`, bound `user_id`, and originating `session_id`;
- originating ingress `run_id` and creation source;
- objective and active constraints;
- lifecycle status and current plan version;
- risk and budget summaries;
- task version for optimistic concurrency;
- created, updated, started, and terminal timestamps.

Task lifecycle states are:

```text
queued -> running -> completed
            |-> waiting_confirmation
            |-> waiting_input
            |-> replanning
            |-> outcome_unknown
            |-> failed
            `-> cancelled
```

`outcome_unknown` is explicit because an external side effect can succeed while
the worker crashes before recording its result.

### 5.2 TaskPlanVersion

Each accepted plan or replan is an immutable version containing:

- `task_id` and monotonically increasing `plan_version`;
- goal, active constraints, and bounded `TaskStep` DAG;
- revision reason and source;
- inherited completed steps and artifact references;
- replaced/invalidated step identifiers;
- invalidated confirmation identifiers;
- validation result and timestamps.

Existing `TaskPlan` and `TaskStep` should be evolved rather than replaced where
their current contracts remain suitable. `PlanValidator` continues to enforce
step limits, unique IDs, known dependencies, known tools, and acyclicity. Durable
validation additionally checks task budgets and whether completed step
inheritance is consistent.

Plans describe actions, candidate tools, dependencies, and input references.
They do not have to freeze final tool arguments. At execution time the LLM uses
the latest prompt-safe task snapshot and artifacts to construct the governed
tool call.

### 5.3 TaskStepRun

A step execution record contains:

- task, plan version, and step identity;
- status and attempt number;
- dependency and input references;
- stable idempotency key;
- lease/worker metadata;
- approval status and bound confirmation digest;
- prompt-safe result, error, and artifact references;
- started and finished timestamps.

Step status distinguishes not-started, ready, leased, running, succeeded,
failed, waiting for confirmation/input, cancelled, and outcome unknown.

### 5.4 TaskEvent and TaskArtifactRef

`TaskEvent` is an append-only, ordered event with a per-task monotonically
increasing cursor. It records task acceptance, plan creation/revision, step
transitions, confirmation requirements, user input, cancellation, recovery, and
terminal outcomes.

`TaskArtifactRef` stores only typed references, prompt-safe summaries, producer
step/version, and trust metadata. It does not duplicate raw tool results, media,
provider payloads, or secrets.

## 6. Component Responsibilities

### Assistant loop

- Selects direct execution or `task_plan_submit` in `auto` mode.
- Must submit a plan first in `durable` mode.
- Generates and revises bounded plans.
- On task resume, chooses only a ready planned step, a valid replan, a request
  for user input, or task completion.
- Produces user-facing summaries without chain-of-thought.

### DurableTaskService

- Owns task state transitions and authorization-scoped access.
- Validates optimistic versions and persists immutable plans.
- Manages leases, checkpoints, budgets, confirmations, cancellation, and event
  ordering.
- Is the source of truth for durable task status.

### Worker

- Claims eligible tasks with bounded leases.
- Builds a resume request from a prompt-safe durable task snapshot.
- Invokes the existing assistant runtime for one short execution quantum.
- Does not independently select or directly invoke business tools.

On resume, trusted tool visibility contains only `task_plan_submit` plus the
tools referenced by currently ready steps. `ActionValidator` independently
checks the bound task, current plan version, ready step, dependency state, and
chosen tool name before execution. Tool visibility improves model guidance but
is not the enforcement boundary.

### Gateway and entry adapters

- Submit foreground requests and return accepted-task results.
- Adapt task query, subscription, confirmation, input, and cancellation for the
  product transport.
- Do not store task truth or schedule worker execution.

### Tool governance

- Revalidates every tool call at execution time.
- Owns risk, confirmation, policy, budget, cancellation, audit, and tool output
  contracts exactly as in the foreground path.
- Treats plan risk labels as display hints, never as authorization.

## 7. Execution and Checkpoint Flow

1. Gateway assigns the ingress turn/run identifiers and enters the current
   assistant runtime.
2. The LLM chooses ordinary ReAct behavior or calls `task_plan_submit`.
3. The plan is schema-validated, passed through `PlanValidator`, and checked
   against task budgets and identity.
4. `DurableTaskService` atomically writes the task, plan v1, and acceptance
   event.
5. Only after that transaction succeeds does the ingress run return an accepted
   response containing `task_id`, plan summary, status, subscription/query
   information, and cancellation support.
6. A worker claims the task and creates a prompt-safe resume request for the
   same assistant loop.
7. One execution quantum performs at most one governed business tool action, one
   plan revision, one user-input/confirmation transition, or one terminal
   transition.
8. Tool observations and state changes are checkpointed before the next quantum
   is scheduled.
9. A crashed worker's lease expires and another worker resumes from the last
   committed checkpoint.

`task_plan_submit` is terminal for its execution quantum. After it succeeds,
the native runtime converts its structured `ToolResult` directly into the
accepted-task response and does not make another LLM call or execute another
business tool in that run. A provider-native batch containing
`task_plan_submit` and any other call is rejected before any call in that batch
executes; the model receives a recovery observation requiring a standalone plan
submission.

During a resume quantum, natural-language final content marks the durable task
completed only when all required plan steps are already succeeded or explicitly
skipped by an accepted revision. Content returned while required work remains
is not treated as completion; the quantum records a bounded protocol failure
and moves to replanning or terminal failure according to the remaining budget.

The scheduler computes the ready set from the current DAG. The first version
uses a default task concurrency of one. The data model may expose multiple ready
read-only steps so bounded parallel execution can be added without changing plan
semantics. Side-effecting or shared-input steps remain serialized unless a later
design proves safe parallel behavior.

The ingress Gateway run ends after durable acceptance. Later execution quanta
have their own internal `run_id` values linked to the stable `task_id`. Product
clients operate on `task_id`; internal run identifiers remain trace/debug facts.

## 8. Confirmation and Risk

Confirmation is evaluated immediately before execution, using the final tool
name and arguments. Low-risk read-only work may execute automatically. A
side-effecting, high-cost, or sensitive call moves the task to
`waiting_confirmation`.

A confirmation record stores a server-computed SHA-256 digest over canonical
JSON containing:

- `task_id`;
- `plan_version`;
- `step_id`;
- normalized tool name and input digest;
- bounded expiry and confirming identity.

Changing the plan version or material tool input invalidates the confirmation.
On rejection, the assistant may skip an optional step, submit a valid revision,
or terminate. It may not rephrase a rejected operation to bypass the policy.

The public API never writes `request.metadata["tool_confirmation"]` directly.
`DurableTaskService` verifies ownership, expiry, plan version, step, tool, and
input digest, then the worker injects a trusted confirmation grant for exactly
one execution quantum. `evaluate_tool_risk` accepts durable confirmation only
from that trusted runtime binding; user-supplied metadata with the same field
names is ignored.

## 9. Failure, Retry, and Cancellation Semantics

Execution is at-least-once with idempotency protection, not general
exactly-once execution.

- Each step has a stable idempotency key derived from stable task/plan/step
  identity and attempt policy.
- The worker injects this key through trusted execution context before the risk
  gate. It does not depend on model-supplied arguments and does not include the
  per-quantum `run_id`.
- Tools that support idempotency receive the key through the governed execution
  context.
- An attempt record is committed before an external call. The result reference
  and final step state are committed after the call.
- If a crash occurs after an external side effect but before result commit, the
  step enters `outcome_unknown` rather than being retried blindly.
- A tool-specific status/reconciliation capability may resolve an unknown
  outcome. Without one, the task waits for user input.

When a lease expires with an attempt left in `running`, read-only tools may be
retried within budget. A step whose canonical policy indicates a possible write
or external side effect moves to `outcome_unknown` unless a durable idempotency
record proves it committed or a tool-specific reconciliation operation resolves
the outcome.

Failure classes are handled as follows:

- Transient failures: bounded exponential backoff under step and task retry
  budgets.
- Invalid input or changed dependency: enter `replanning` and request a new plan
  version.
- Policy rejection: wait for confirmation, change the goal, or terminate; a
  replan cannot weaken policy.
- Provider context overflow: retain the current compaction and single retry
  behavior.
- Exhausted revision/tool/cost/time budget: produce a partial terminal result
  that names completed and unfinished work.

Cancellation is cooperative. Queued/unstarted steps stop immediately; active
tools receive the existing cancel token. Late results after cancellation are
audit or stale-artifact data and cannot overwrite the visible terminal state.
Completed side effects are never described as rolled back. A compensation step
is allowed only when a tool explicitly supports compensation, and it passes
normal validation and confirmation.

## 10. Persistence and Concurrency

Introduce a replaceable `TaskStore` interface.

- SQLite is the local-first default implementation.
- An in-memory implementation supports unit tests.
- A future PostgreSQL implementation is outside the first version.

Task mutations use transactions plus optimistic task versions. Task events and
the corresponding aggregate mutation commit atomically. Lease acquisition uses
a compare-and-set style transition with owner and expiry. A stale worker cannot
commit after losing its lease without satisfying the current task version and
lease token.

The existing Gateway queue remains process-local admission state. It neither
stores durable tasks nor supplies restart recovery.

## 11. API and User-visible Progress

The original chat/agent endpoint can create a task. The Gateway ingress run
remains a completed run in the stable v1 protocol. Task acceptance is represented
inside its response data rather than overloading the run lifecycle status:

```json
{
  "status": "completed",
  "data": {
    "task": {
      "submission_status": "accepted",
      "task_id": "task_...",
      "task_status": "queued",
      "plan_version": 1,
      "plan_summary": {},
      "progress_url": "/tasks/task_.../events",
      "cancel_supported": true
    }
  }
}
```

Add authorization-scoped task operations equivalent to:

```text
GET  /tasks/{task_id}
GET  /tasks/{task_id}/events?after=<cursor>
POST /tasks/{task_id}/confirmations
POST /tasks/{task_id}/input
POST /tasks/{task_id}/cancel
```

User-visible event types include:

- `task.accepted`;
- `plan.created` and `plan.revised`;
- `step.started`, `step.completed`, and `step.failed`;
- `confirmation.required` and `task.waiting_input`;
- `task.completed`, `task.failed`, and `task.cancelled`.

Events use monotonic cursors so a disconnected client can replay missed
progress. Gateway/WebSocket adapters may project records as `task.*` frames,
but `DurableTaskService` remains the source of truth. Progress text and plan
reasons are concise, high-level, and auditable; they never contain hidden model
reasoning.

## 12. Context Boundary

Each execution quantum receives a `DurableTaskSnapshot` as a distinct
`AssistantContextPack` section containing only:

- objective and active constraints;
- current immutable plan version and ready steps;
- compact completed-step summaries;
- artifact/output references;
- unresolved errors, confirmation, or user input;
- remaining task, tool, provider, cost, and time budgets.

The snapshot does not include full parent conversation history, raw tool
results, provider payloads, media bodies, secrets, or arbitrary request
metadata. Original material stays in its owning store and is referenced through
prompt-safe identifiers.

Task state is current execution context, not long-term memory. Completion may
create a normal memory promotion candidate, but it does not automatically write
long-term memory or bypass `MemoryWritePolicy`.

## 13. Validation Strategy

### Unit and schema tests

- Plan cycles, duplicate IDs, unknown dependencies/tools, and step limits.
- Lifecycle transition legality and terminal-state uniqueness.
- Plan revision inheritance and confirmation invalidation.
- Task identity binding, optimistic versions, and budget enforcement.

### Store and service tests

- SQLite transaction and restart recovery.
- Atomic aggregate/event writes and monotonic cursors.
- Lease expiry, takeover, stale-worker rejection, and concurrent mutation.
- Idempotency keys and `outcome_unknown` transitions.

### Assistant runtime tests

Use scripted/fake non-mock chat adapters to prove:

- simple `auto` requests do not submit tasks;
- complex `auto` requests may submit a valid task;
- `durable` requests cannot call business tools before plan submission;
- resume snapshots drive the next ready step;
- tool failure can produce a bounded plan revision;
- completion produces a user-facing summary from persisted evidence.

### Governance and integration tests

- Async calls retain validator, executor, registry, budget, approval, audit, and
  cancellation behavior.
- The ingress run returns accepted only after durable commit.
- Task query, replay, confirmation, user input, and cancel are identity-scoped.
- Client reconnect can resume event consumption by cursor.
- Context reports show the task section without leaking raw observations,
  memory, conversation, or provider data.

### Failure-injection tests

Crash at these boundaries:

- before tool execution;
- after external success but before result commit;
- after checkpoint but before scheduling the next quantum.

The tests must prove safe retry, `outcome_unknown`, and no duplicate visible
terminal transition respectively.

## 14. Acceptance Criteria

- Default simple requests add no provider call beyond the existing ReAct call
  and create no durable task row.
- A `durable` request produces a valid plan before any business tool call.
- The ingress run returns accepted only after the task transaction commits.
- A process restart resumes from the latest committed checkpoint.
- Ordinary lease takeover does not repeat an already committed side effect.
- Uncertain external side effects enter `outcome_unknown` and are not retried
  automatically.
- Only the bound user can read, confirm, provide input to, or cancel a task.
- Progress is replayable, ordered, and has exactly one visible terminal state.
- Plan revisions, tool calls, retries, cost, and time are locally bounded.
- No plan, model output, or task metadata can bypass tool governance.

## 15. Rollout

Ship behind a disabled feature flag. Validate schemas, SQLite recovery,
scripted/fake-real LLM behavior, Gateway/API contracts, and offline evals first.
Real external LLM testing is explicit opt-in under `provider_smoke` or `pilot`.
The presence of an API key never enables the feature or a real provider.

The first release uses SQLite, a single-host worker, default per-task concurrency
of one, and bounded DAG semantics. PostgreSQL, multi-node scheduling, and
parallel step execution require separate follow-up designs and evidence.

The initial configuration contract is:

- `MULTIMODAL_AGENT_DURABLE_TASKS_ENABLED=false`;
- `MULTIMODAL_AGENT_DURABLE_TASK_PATH=.local/tasks/durable_tasks.sqlite3`;
- `MULTIMODAL_AGENT_DURABLE_TASK_WORKER_ENABLED=false`;
- `MULTIMODAL_AGENT_DURABLE_TASK_LEASE_SECONDS=30`;
- `MULTIMODAL_AGENT_DURABLE_TASK_POLL_SECONDS=1.0`.

Enabling the store/tool without the worker is valid for API and deterministic
`run_once()` tests. The FastAPI lifespan starts the local worker only when both
feature and worker flags are true, and shuts it down cooperatively before the
Gateway runtime is closed.
