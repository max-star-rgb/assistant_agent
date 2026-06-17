---
name: phase7a-runner
description: "Runs Phase 7A Runtime Configuration Profiles tasks 124-127, using safe local/offline defaults."
version: "1.0.0"
---

# Skill: Phase 7A Runtime Configuration Profiles Runner

## Purpose

Run tasks 124-127, then stop.

## Read first

```text
AGENTS.md
docs/126-phase7a-runtime-configuration-profiles.md
tasks/README_PHASE7.md
```

## Task sequence

```text
124 Runtime Profile Schema and Defaults
125 Wire Runtime Profile into ProviderConfig
126 Runtime Profile Safety Tests
127 Runtime Profile Docs and Review
```

## Rules

- Complete one task before starting the next.
- Do not start the next track.
- Default mock/local/offline.
- Do not call real Providers by default.
- Do not write API keys.
- Prefer apply_patch.
- Stop after Task 127.
