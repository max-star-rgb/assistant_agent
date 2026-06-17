---
name: phase7b-runner
description: "Runs Phase 7B Real Provider Production Hardening tasks 128-131, using safe local/offline defaults."
version: "1.0.0"
---

# Skill: Phase 7B Real Provider Production Hardening Runner

## Purpose

Run tasks 128-131, then stop.

## Read first

```text
AGENTS.md
docs/127-phase7b-real-provider-production-hardening.md
tasks/README_PHASE7.md
```

## Task sequence

```text
128 Real Provider Config Validation
129 Provider Readiness Checks and Smoke Contract
130 Provider Diagnostics and Safety Defaults
131 Phase 7B Review
```

## Rules

- Complete one task before starting the next.
- Do not start the next track.
- Default mock/local/offline.
- Do not call real Providers by default.
- Do not write API keys.
- Prefer apply_patch.
- Stop after Task 131.
