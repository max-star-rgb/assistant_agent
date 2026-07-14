# Assistant Agent Documentation Sync Skill Design

## Status

Approved for implementation planning on 2026-07-14.

## Goal

Create a project-local skill that updates the `assistant_agent` documentation system after the user explicitly requests a documentation sync or drift audit. The skill must reconcile repository facts with `README.md`, `AGENTS.md`, authority documents, retained runbooks, walkthroughs, API references, interview material, and project-local skills. It may remove obsolete documents and introduce new authority documents when the evidence and long-term ownership justify them.

The skill is not an automatic post-development or post-merge hook. Repository changes alone do not authorize it to run.

## Location And Invocation

Create the skill at:

```text
.codex/skills/assistant-agent-documentation-sync/
```

Use it only when the user explicitly asks to update or reconcile project documentation, audit documentation drift, clean obsolete documents, or explicitly names the skill. Do not add `policy.allow_implicit_invocation` to `agents/openai.yaml`; express the explicit-request boundary in the skill name, description, and instructions while following the repository's existing skill metadata conventions.

Once invoked, the default mode is to perform justified edits rather than stop after producing an audit report. Report uncertain deletion candidates without deleting them.

## Design Options

### Prompt-only skill

A single `SKILL.md` could describe the audit commands and judgment rules. This is lightweight but makes Git-range collection, path validation, link checks, and evidence formatting inconsistent between runs.

### Skill with deterministic evidence collection

The selected approach combines a concise `SKILL.md` with a bundled evidence-collection script. The script performs mechanical discovery and validation; the agent remains responsible for semantic classification, authority selection, writing, and deletion decisions.

### CI-enforced documentation contracts

Continuous enforcement could eventually detect stale paths and contract violations, but the repository does not yet have stable enough documentation contracts to avoid noisy checks. This is outside the current scope.

## Documentation Model

Classify every relevant document into one of these roles:

| Role | Purpose | Default action |
| --- | --- | --- |
| Repository entry | `AGENTS.md` for coding agents and `README.md` for humans | Keep concise and route to authority |
| Authority document | Current architecture, ownership, boundaries, source map, and validation | Update or add when a stable boundary requires it |
| Operational runbook | Commands and procedures that remain usable by an operator | Retain while executable and needed |
| Reference or walkthrough | API contracts, implementation explanations, and project-owner walkthroughs | Retain only while accurate and discoverable |
| Interview material | Training content isolated under `docs/interview/**` | Update only when affected or explicitly requested |
| Development artifact | Specs, plans, roadmaps, and temporary execution material | Never treat as current authority; retain only with clear ongoing value |
| Obsolete document | Superseded, contradictory, non-executable, duplicated, or ownerless content | Delete when evidence is conclusive |

`docs/development/**` receives explicit lifecycle review. Being in that directory is not sufficient reason to retain a file. The audit also covers other documentation directories, including old specs, plans, and roadmaps, while respecting any current workflow that still depends on them.

## Evidence Sources

Use repository evidence in this order:

1. Current source code and public contracts.
2. Tests that establish supported behavior and default states.
3. Runtime configuration, package metadata, scripts, and real command entrypoints.
4. Current authority documents where they agree with implementation.
5. Git history and diffs for change localization, not as the sole description of current behavior.
6. Development plans and historical documents only as provenance, never as current truth without corroboration.

Inspect both the requested or inferred Git change range and the current documentation system as a whole. A narrow recent diff must not hide older drift in linked or overlapping documents.

## Workflow

### 1. Establish scope and preserve user work

- Read `AGENTS.md` and inspect the worktree before editing.
- Identify user changes and leave unrelated modifications untouched.
- Determine the requested change range when one is supplied; otherwise use Git history only to localize likely increments.
- Announce the expected documentation surfaces before making edits.

### 2. Collect deterministic evidence

Run the bundled audit script to collect:

- documentation inventory and role candidates;
- recent code, test, config, script, and documentation changes;
- Markdown links and referenced repository paths;
- inbound references to deletion candidates;
- last-touch commit metadata;
- existing authority and project-skill routing.

The script must produce a report only. It must not rewrite, move, or delete repository files.

### 3. Build a capability-to-authority map

Map each implemented capability or changed boundary to:

- its owning source layer;
- supporting tests and configuration;
- its current authority document, if any;
- affected human and agent entrypoints;
- affected project-local skills.

Prefer updating an existing authority document when the capability belongs to an established boundary.

### 4. Update or create authority

Create a new authority document only when all of the following are true:

- the boundary is implemented and stable enough to document as current behavior;
- no existing authority document can own it without becoming incoherent;
- its owning layer and source map are identifiable;
- its validation entrypoints are real;
- its update rules and relationship to neighboring authorities are explicit.

When adding authority, update `AGENTS.md`, `README.md`, and any relevant project-local skill so the authority is discoverable. Do not create an authority document solely because a development plan exists.

### 5. Reconcile entrypoints and skills

- Keep `README.md` a concise human navigation and local-run entrypoint.
- Keep `AGENTS.md` a stable repository routing and boundary document.
- Route specialized work through `.codex/skills/**` without copying full architecture into skills.
- Update an existing specialty skill when its source map, authority routes, validation, or working rules changed.
- Create a new specialty skill only when a newly established authority represents a distinct recurring work domain.

### 6. Retire obsolete documents

Delete a document only when repository evidence establishes that it is obsolete and no unique long-term value remains. Before deletion, verify:

- current behavior is documented elsewhere or no longer exists;
- no valid entrypoint depends on the document;
- no retained runbook procedure remains executable only there;
- no unique API contract or operational warning would be lost;
- references can be removed or redirected without ambiguity.

If any condition is uncertain, retain the file and list it as a review candidate with the missing evidence.

### 7. Validate the result

Always run:

- the skill structural validator;
- the evidence script's link and path checks;
- `git diff --check` over changed documentation and skill files.

Run the smallest relevant project tests when edited documentation asserts commands, defaults, API behavior, or runtime contracts. Do not run external providers merely to validate documentation.

## Bundled Script Boundary

Add a small script under the skill's `scripts/` directory. It should accept the repository root and optional Git range, then emit a stable Markdown or JSON evidence report. It may use Git and filesystem inspection to inventory files, references, and changes.

The script must not:

- decide whether prose is semantically correct;
- infer architecture ownership without presenting source evidence;
- edit documentation;
- delete or move files;
- invoke network services or external providers.

This separation keeps mechanical checks repeatable while preserving human-reviewable judgment for documentation meaning and lifecycle decisions.

## Skill Validation Strategy

Develop the skill using a RED-GREEN-REFACTOR loop:

1. Give a fresh agent a representative documentation-sync request without the new skill and record omissions such as narrow diff-only review, failure to inspect obsolete docs, or duplication between README and AGENTS.
2. Implement the minimum skill and evidence script that address observed failures.
3. Repeat the scenario with the skill and verify correct authority routing, lifecycle review, and evidence-backed edits.
4. Tighten instructions only for demonstrated gaps.

Also test the explicit-request boundary: a normal implementation request must not cause the agent to launch a repository-wide documentation audit merely because the skill exists.

## First Use On The Current Repository

The first execution will apply the skill to the current incremental state. It will examine at least:

- local memory intelligence and framework bake-off boundaries;
- realtime semantic interrupt arbitration;
- deterministic proactive wake;
- offline Improvement Lab;
- realtime video context and latency observability;
- the existing authority set and every retained `docs/development/**` document;
- stale development plans, roadmaps, references, and navigation across the wider `docs/**` tree.

This list seeds investigation but does not predetermine which files are updated or deleted. Final edits require current source, tests, configuration, link, and ownership evidence.

## Completion Criteria

The work is complete when:

- the project-local skill and metadata validate;
- its evidence script is covered by tests and passes them;
- current capabilities map to coherent authority documents;
- `README.md`, `AGENTS.md`, authority docs, and relevant specialty skills agree;
- clearly obsolete documents are removed and all references repaired;
- uncertain deletion candidates are reported rather than silently removed;
- documentation commands, paths, defaults, and validation claims are verified;
- the final report lists edits, deletions, tests, limitations, and remaining review candidates.
