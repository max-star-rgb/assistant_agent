# Task 104 Skills Packaging Structure and Skill Templates

## Goal

Create the `skills/` directory structure and `SKILL.md` templates.

## Read first

- `docs/110-skills-packaging-structure.md`
- `AGENTS.md`
- existing `skills/` directory

## Scope

- Add skill package structure for this repo.
- Add `SKILL.md` templates with YAML frontmatter.
- Add offline skill validation script if missing.
- Do not include secrets, real user data, raw Provider output, or media assets.

## Acceptance

```bash
python scripts/validate_skills.py
python -m pytest
```
