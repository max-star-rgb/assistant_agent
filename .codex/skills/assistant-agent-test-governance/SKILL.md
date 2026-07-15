---
name: assistant-agent-test-governance
description: Use only when the user explicitly requests an assistant_agent test audit, deduplication, layering or marker review, test-suite cleanup, or explicitly names this skill.
---

# Assistant Agent Test Governance

Govern tests from repository evidence. Do not expand ordinary feature work into a repository-wide test audit.

## Workflow

1. Read `AGENTS.md` and `tests/README.md`; inspect dirty, staged, and range changes without touching unrelated work.
2. Run the evidence collector with `--profile none`. Add `--git-range` only when the user supplies or the task defines a meaningful range.
3. Inspect each candidate's layer, effective markers, imports/targets, inbound references, last-touch history, assertions, fixtures, failure mode, and retained replacement. The collector's exact-duplicate output is a candidate list, not deletion authorization.
4. Classify each item:
   - **Keep** unique contracts, safety boundaries, historical regressions, compatibility behavior, and failure/recovery evidence.
   - **Merge** tests in the same layer only when setup, condition, failure mode, and assertions are equivalent; preserve small diagnostic cases or parameterize them.
   - **Reclassify** markers, paths, or names that disagree with actual layer/cost without changing the protected behavior.
   - **Delete** only removed behavior or a test whose assertions and boundaries are completely covered by named retained tests.
5. Report uncertain candidates without modifying them. Test count, coverage percentage, age, and runtime are never sufficient deletion reasons by themselves.
6. Before edits, record the candidate-to-retained-test mapping. After edits, synchronize markers, shared fixtures/builders, and `tests/README.md`; do not create a large test that obscures failures.
7. Run the affected tests, `pytest -m fast -q`, the full offline suite, the collector again, Skill validation, and `git diff --check`. Never enable integration or a real provider for profiling.

## Evidence Collector

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-test-governance/scripts/collect_test_evidence.py \
  --repo-root . [--git-range BASE..HEAD] [--profile none|fast|full-offline]
```

The command emits one JSON document to stdout and does not edit repository files. `fast` runs `-m fast`; `full-offline` runs `-m "not integration"` under the mock runtime profile. A failing profile is evidence in `profile_run.pytest_exit_code`, not a collector failure.

Validate this Skill with:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/assistant-agent-test-governance
```

## Decision Gate

Delete or merge only when the retained test is named and all of these are proven: same supported behavior, same boundary and failure mode, no unique assertion or fixture semantics, no historical regression/compatibility purpose, and post-change offline verification passes. Otherwise keep or report the candidate.

## Common Mistakes

| Mistake | Required response |
| --- | --- |
| Delete to hit a count or time target | Treat the metric as context, not authorization. |
| Equate identical AST with identical value | Inspect history, fixtures, layer, and failure purpose. |
| Require coverage or mutation tooling | Use available evidence; never make a percentage the sole gate. |
| Merge many cases into one test | Preserve focused failure localization and named regression cases. |
| Profile integration tests | Stop and use an offline profile. |
