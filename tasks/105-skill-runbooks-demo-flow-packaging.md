# Task 105 Skill Runbooks and Demo Flow Packaging

## Goal

Add runbooks and reusable resources for the main skills.

## Read first

- `docs/111-skill-runbooks-and-demo-flow-packaging.md`
- current demo runner
- current smoke scripts
- current eval suite

## Scope

- Add skill runbooks.
- Add safe demo flow packaging references.
- Keep all commands offline.
- Do not add real Provider output samples.

## Acceptance

```bash
python scripts/validate_skills.py
python -m pytest
```
