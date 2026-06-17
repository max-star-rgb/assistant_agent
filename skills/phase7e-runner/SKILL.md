---
name: phase7e-runner
description: "Runs Phase 7E Deployment Readiness tasks 140-143, using safe local/offline defaults."
version: "1.0.0"
---

# Skill: Phase 7E Deployment Readiness Runner

## Purpose

Run tasks 140-143, then stop.

## Read first

```text
AGENTS.md
docs/130-phase7e-deployment-readiness.md
tasks/README_PHASE7.md
```

## Task sequence

```text
140 Docker / Compose Verification
141 Deployment Config and Persistent Paths
142 Healthcheck / Readiness / Backup Runbook
143 Phase 7E Review
```

## Rules

- Complete one task before starting the next.
- Do not start the next track.
- Default mock/local/offline.
- Do not call real Providers by default.
- Do not write API keys.
- Prefer apply_patch.
- Stop after Task 143.
