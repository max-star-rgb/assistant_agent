# 110 Skills Packaging Structure

## Goal

Provide a repository-local `skills/` structure for reusable Codex workflows. Skills in this repository are packaging instructions and runbooks; they do not add new assistant capabilities.

## Required Structure

Each skill directory should contain:

```text
skills/<skill-name>/
├── SKILL.md
├── README_USE_THIS_SKILL.md optional
├── prompts/ optional
└── resources/ optional
```

Every `SKILL.md` must begin with YAML frontmatter:

```markdown
---
name: example-skill
description: "Short description."
version: "1.0.0"
---
```

## Safety

Skills must not contain:

- API keys
- Authorization headers
- Bearer tokens
- `.env` secrets
- real user memory
- real media
- raw Provider responses
- generated images or render artifacts
- commands that publish remote MCP services

## Validation

The offline validator is:

```bash
python scripts/validate_skills.py
```

It checks:

- every `SKILL.md` has frontmatter
- required frontmatter fields exist
- skill names match safe naming rules
- obvious secrets are not present
- unsafe publish/deploy wording is not present without clear prohibition context

## Initial Skill Packages

Phase 5J keeps existing runner skills and adds lightweight reusable skill templates:

- `assistant-demo-flow`
- `offline-mcp-tools`

These skills provide instructions for offline demo and MCP smoke workflows only.
