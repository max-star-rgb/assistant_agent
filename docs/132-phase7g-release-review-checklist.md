# 132 Phase 7G Release Review Checklist

## Goal

Audit whether the project is ready for a controlled real usage pilot.

## Required Report

Generate:

```text
docs/133-phase7-production-readiness-review.md
```

## Review Checklist

- `local_demo` remains default.
- `offline_eval` remains offline.
- Real Provider paths are explicit and gated.
- API keys are not committed.
- Run/trace/error outputs are redacted.
- Web product surface is usable.
- User/session boundaries protect run, trace, and memory queries.
- Deployment runbook is accurate.
- Pilot feedback loop exists.

## Required Commands

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
python scripts/run_demo_flows.py
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
git status --short
```
