# 125 Phase 7 Roadmap: Production Readiness / Real Usage Pilot

## Background

Phase 6 has completed the usable local demo:

- local CLI
- FastAPI demo endpoints
- simple Web Console
- offline demo flows
- optional real Provider setup docs
- local deployment files
- local observability docs
- consolidated user documentation

Phase 7 should move from "usable demo" toward "controlled real usage pilot".

The goal is not to add many new Agent capabilities. The goal is to make the existing assistant safer, more configurable, easier to operate, and ready for limited real-user testing.

## Phase 7 Goal

Prepare the project for a small, controlled real usage pilot:

```text
real user request
  -> authenticated local/server API
  -> explicit Provider configuration
  -> safe Agent execution
  -> traceable results
  -> bounded cost
  -> recoverable errors
  -> deployable service
```

## Recommended Phase 7 Tracks

```text
Phase 7A: Runtime Configuration Profiles
Phase 7B: Real Provider Production Hardening
Phase 7C: Web Productization Baseline
Phase 7D: Auth / User / Session Boundary
Phase 7E: Deployment Readiness
Phase 7F: Pilot Evaluation / Feedback Loop
Phase 7G: Phase 7 Release Review
```

## Phase 7A: Runtime Configuration Profiles

### Goal

Define clear runtime modes:

```text
local_demo
offline_eval
provider_smoke
pilot
```

### Scope

- Add a typed runtime profile config.
- Keep `local_demo` as default.
- Make real Provider usage explicit.
- Document env precedence.
- Prevent accidental real Provider calls in tests/evals/demo.

### Out of Scope

- No new Provider.
- No cloud secrets manager yet.
- No production auth.

## Phase 7B: Real Provider Production Hardening

### Goal

Make opt-in real Providers safer and more consistent for pilot use.

### Scope

- Normalize real Provider config validation.
- Add provider capability readiness checks.
- Add smoke result contract validation.
- Add stronger timeout/retry/cost defaults per Provider family.
- Add redacted provider diagnostic summaries.

### Out of Scope

- No default real Provider calls.
- No mass integration with many providers.
- No committed real outputs.

## Phase 7C: Web Productization Baseline

### Goal

Upgrade the simple Web Console into a minimal usable product surface.

### Scope

- Keep frontend lightweight.
- Add scenario picker improvements.
- Add request history for current browser session.
- Add trace/run detail panels.
- Add clearer loading/error states.
- Add capability badges and tool-call timeline.

### Out of Scope

- No complex frontend framework unless explicitly chosen.
- No payment, order, checkout, crawling, or marketplace behavior.
- No production analytics SDK.

## Phase 7D: Auth / User / Session Boundary

### Goal

Introduce a minimal user/session boundary for pilot safety.

### Scope

- Define local/pilot auth model.
- Add user/session ownership checks for run and trace queries.
- Add API key or simple bearer-style pilot token support if needed.
- Keep secrets out of repo.
- Document local-only and pilot-only behavior.

### Out of Scope

- No full enterprise IAM.
- No OAuth provider unless explicitly required.
- No multi-tenant billing.

## Phase 7E: Deployment Readiness

### Goal

Make the service deployable on a single small server.

### Scope

- Verify Docker build.
- Add production-like env template without secrets.
- Add persistent local paths for memory/trace if needed.
- Add process runbook.
- Add backup/restore notes for local state.
- Add healthcheck and readiness checks.

### Out of Scope

- No Kubernetes.
- No distributed queue.
- No production observability stack unless required.

## Phase 7F: Pilot Evaluation / Feedback Loop

### Goal

Measure whether the assistant is useful and safe for limited users.

### Scope

- Add pilot scenario set.
- Add manual feedback schema.
- Add failure taxonomy for real user runs.
- Add review script for redacted run summaries.
- Add acceptance thresholds for pilot readiness.

### Out of Scope

- No automated collection of private user content.
- No raw media or raw Provider response storage.
- No hidden telemetry.

## Phase 7G: Phase 7 Release Review

### Goal

Audit whether the project is ready for a controlled pilot.

### Review Checklist

- Default local demo still works offline.
- Tests/evals/demo do not call real Providers by default.
- Real Provider paths are explicit and gated.
- API keys are not committed.
- Run/trace/error outputs are redacted.
- Web product surface is usable for core flows.
- Deployment runbook is accurate.
- Known limitations are documented.

## Recommended Execution Order

```text
7A -> 7B -> 7C -> 7D -> 7E -> 7F -> 7G
```

Reasoning:

1. Configuration profiles should come first, because every later production/pilot behavior depends on clear runtime mode.
2. Real Provider hardening should happen before real user pilot work.
3. Web productization should happen before auth/session restrictions are finalized, so the actual UI flow is known.
4. Auth/session boundaries should happen before server deployment.
5. Deployment readiness should happen before pilot evaluation.
6. Pilot evaluation should happen before the Phase 7 release review.

## Phase 7 Default Safety Rules

- Default mode remains `local_demo`.
- Default tests remain offline.
- Default evals remain offline.
- Default demo flows remain offline.
- Real Provider calls require explicit runtime profile and env config.
- No API keys in repo.
- No real user data committed.
- No real media committed.
- No generated images or render artifacts committed.
- No raw Provider responses committed.
- No hidden telemetry.

## Suggested First Task

Start with:

```text
Phase 7A Runtime Configuration Profiles
```

First concrete task:

```text
Task 124 Runtime Profile Schema and Defaults
```

Goal:

- Define runtime profiles.
- Keep local demo default.
- Make test/eval/demo behavior explicitly offline.
- Add tests proving real Provider modes are opt-in only.

## Phase 7 Done Definition

Phase 7 is complete when:

- local demo remains fully functional
- pilot runtime mode is explicit
- real Provider usage is gated and validated
- API/Web flows are usable for a small pilot
- user/session boundaries protect run and trace queries
- single-server deployment is documented and verified
- pilot feedback and review workflow exists
- final Phase 7 review passes

| 阶段 | 主题                 | 解决的问题                     |
| -- | ------------------ | ------------------------- |
| 7A | Runtime Profiles   | 当前运行模式是什么，能不能调真实 Provider |
| 7B | Provider Hardening | 真实 API 怎么安全接入和诊断          |
| 7C | Web Productization | Web demo 怎么变得可试用          |
| 7D | Auth / Session     | 小范围试用时怎么隔离用户数据            |
| 7E | Deployment         | 怎么部署到一台服务器                |
| 7F | Feedback Loop      | 真实试用后怎么收集问题和评估            |
| 7G | Release Review     | 是否可以进入 controlled pilot   |
