---
name: phase7d-runner
description: "Runs Phase 7D Auth / User / Session Boundary tasks 136-139, using safe local/offline defaults."
version: "1.0.0"
---

# Skill: Phase 7D Auth / User / Session Boundary Runner

## Purpose

Run tasks 136-139, then stop.

## Read first

```text
AGENTS.md
docs/129-phase7d-auth-user-session-boundary.md
tasks/README_PHASE7.md
```

## Task sequence

```text
136 Auth Mode and Pilot Token Boundary
137 Run / Trace / Memory Ownership Checks
138 Auth Docs and Safety Tests
139 Phase 7D Review
```

## Rules

- Complete one task before starting the next.
- Do not start the next track.
- Default mock/local/offline.
- Do not call real Providers by default.
- Do not write API keys.
- Prefer apply_patch.
- Stop after Task 139.
