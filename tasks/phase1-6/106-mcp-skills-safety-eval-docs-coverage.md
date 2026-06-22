# Task 106 MCP / Skills Safety, Eval, and Docs Coverage

## Goal

Add offline safety validation, tests, docs coverage, and optional packaging eval.

## Read first

- `docs/112-mcp-skills-safety-eval-plan.md`
- current MCP skeleton
- current skills packages
- current eval runner

## Scope

- Add MCP / Skills safety tests.
- Add docs coverage.
- Add packaging eval suite if lightweight.
- Do not call real Providers.
- Do not publish remote MCP services.

## Acceptance

```bash
python scripts/smoke_mcp_tools.py
python scripts/validate_skills.py
python -m pytest
```

If implemented:

```bash
python scripts/run_evals.py --suite packaging
```
