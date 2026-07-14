---
name: assistant-agent-documentation-sync
description: Use when the user explicitly requests an assistant_agent repository documentation sync, documentation drift audit, authority reconciliation, or obsolete-document cleanup, or explicitly names this skill.
---

# Assistant Agent Documentation Sync

Use this skill only after an explicit documentation-sync request. Repository changes alone do not authorize a repository-wide audit.

## Workflow

1. Read `AGENTS.md`; inspect `git status --short`, `git diff --name-status`, and
   `git diff --cached --name-status`; identify unrelated user work. The collector's
   `git_changes` covers only `--git-range`, not dirty or staged changes.
2. Read the relevant project specialty skills and their authority documents.
3. Run the bundled collector once without `--git-range` for the complete inventory and once with the user-supplied range. If no range was supplied, do not invent one; Git history may only localize investigation.
4. Map implemented capabilities to owning source, tests/config, authority, `README.md`, `AGENTS.md`, and specialty skills.
5. Update existing authority where possible. Create a new authority only for an implemented, stable, separately owned boundary with real validation entrypoints.
6. Review `docs/development/**`, walkthroughs, references, roadmaps, and `docs/superpowers/**`. Specs and plans are development records, not current authority.
7. Delete only when a replacement authority exists, no unique operational/API/compatibility value remains, all inbound references can be repaired, and source/test/history evidence is conclusive. Otherwise report a candidate without deleting it.
8. Re-run the collector, skill validation, relevant offline tests, and `git diff --check`. Never call a real provider for documentation validation.

## Evidence Collector

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root . [--git-range BASE..HEAD]
```

The command emits JSON to stdout and never edits files. Treat missing links and paths as structured evidence: distinguish real drift from examples, globs, historical records, and external layouts before editing.

Validate project skills with:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/<skill-name>
```

## Boundaries

- Current source and tests outrank prose and Git history.
- Keep `README.md` human-oriented and `AGENTS.md` routing-oriented.
- Keep specialty skills concise; route them to authority rather than copying architecture.
- Preserve unrelated dirty files and do not bulk-delete historical plans/specs.
- Report edits, deletions, tests, limitations, and uncertain candidates.
