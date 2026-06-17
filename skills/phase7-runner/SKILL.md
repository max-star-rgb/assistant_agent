---
name: phase7-runner
description: "Automatically runs Phase 7 Production Readiness tasks from Task 124 through Task 148, using explicit runtime profiles and stopping after the Phase 7 review."
version: "1.0.0"
---

# Skill: Phase 7 Production Readiness Runner

## Purpose

Run Phase 7 tasks from Task 124 through Task 148 in order, then stop.

## Warning

Phase 7 is long. Prefer running by track if possible. Use this full runner only when the user explicitly asks to run the entire Phase 7 automatically.

## Task sequence

```text
124 Runtime Profile Schema and Defaults
125 Wire Runtime Profile into ProviderConfig
126 Runtime Profile Safety Tests
127 Runtime Profile Docs and Review
128 Real Provider Config Validation
129 Provider Readiness Checks and Smoke Contract
130 Provider Diagnostics and Safety Defaults
131 Phase 7B Review
132 Web Console UX Baseline
133 Web Run / Trace Detail Panels
134 Web Error and Loading States
135 Phase 7C Review
136 Auth Mode and Pilot Token Boundary
137 Run / Trace / Memory Ownership Checks
138 Auth Docs and Safety Tests
139 Phase 7D Review
140 Docker / Compose Verification
141 Deployment Config and Persistent Paths
142 Healthcheck / Readiness / Backup Runbook
143 Phase 7E Review
144 Pilot Scenario Set and Feedback Schema
145 Redacted Run Review and Failure Taxonomy
146 Pilot Acceptance Thresholds
147 Phase 7F Review
148 Phase 7G Production Readiness Review
```

## Hard Rules

- Do not start Phase 8.
- Do not call real Providers by default.
- Do not write API keys.
- Do not create secret `.env` files.
- Do not commit real user data, real media, generated outputs, rendered outputs, or Provider raw responses.
- Do not install dependencies from the network.
- Do not retry without sandbox.
- Do not deploy to public internet.
- Do not implement Kubernetes.
- Default runtime profile remains `local_demo`.
- Default tests/evals/demo remain offline.

## Read first

```text
AGENTS.md
docs/125-phase7-production-readiness-roadmap.md
docs/126-phase7a-runtime-configuration-profiles.md
tasks/README_PHASE7.md
```

## Execution Rules

- Complete one task before starting the next.
- Respect each task Scope.
- Run acceptance checks for each task.
- Fix only failures caused by the current or immediately previous task.
- If a blocking failure is unrelated, document it and stop.
- Stop after Task 148.
