---
name: phase7f-runner
description: "Runs Phase 7F Pilot Evaluation / Feedback Loop tasks 144-147, using safe local/offline defaults."
version: "1.0.0"
---

# Skill: Phase 7F Pilot Evaluation / Feedback Loop Runner

## Purpose

Run tasks 144-147, then stop.

## Read first

```text
AGENTS.md
docs/131-phase7f-pilot-evaluation-feedback-loop.md
tasks/README_PHASE7.md
```

## Task sequence

```text
144 Pilot Scenario Set and Feedback Schema
145 Redacted Run Review and Failure Taxonomy
146 Pilot Acceptance Thresholds
147 Phase 7F Review
```

## Rules

- Complete one task before starting the next.
- Do not start the next track.
- Default mock/local/offline.
- Do not call real Providers by default.
- Do not write API keys.
- Prefer apply_patch.
- Stop after Task 147.
