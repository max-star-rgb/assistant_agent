# Improvement Lab Design

Date: 2026-07-14

## Objective

Add an offline engineering loop that discovers recurring assistant failures and
automatically produces evidence-backed improvement proposals for skills,
runtime behavior, and code. The first version stops at human review. It does
not edit production files, change runtime configuration, create commits, open
pull requests, or deploy candidates.

The desired loop is:

```text
redacted runtime evidence + eval/test evidence
  -> deterministic opportunity detection
  -> structured proposal generation
  -> local constraint evaluation
  -> ranked review candidates
  -> human decision
```

This design borrows the useful workflow shape from
[Hermes Agent Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution):
optimization runs outside the production agent, uses execution evidence and
evaluation data, generates candidates, applies constraint gates, and ends in
human review. It does not copy Hermes Agent's runtime, tool system, mutable
skill execution model, DSPy/GEPA dependency stack, or planned continuous
self-modification loop.

## Scope Decision

Version 1 detects opportunities for all three target types:

- `skill`: repo-local `skills/<skill_id>/SKILL.md` capability descriptors;
- `runtime`: context, loop, tool-governance, retry, budget, and provider adapter
  behavior;
- `code`: concrete Python modules or symbols involved in a repeated failure.

Version 1 may generate a unified-diff preview only for a skill target. Runtime
and code targets produce a diagnosis, proposed change, affected locations,
acceptance criteria, and suggested tests, but no code patch.

## Design Principles

- Improvement is a control-plane engineering workflow, not a new assistant-loop
  node.
- Evidence is immutable input. Proposals never rewrite their supporting
  evidence.
- Every proposal cites evidence IDs. Unsupported advice is rejected rather
  than presented as an improvement candidate.
- Detection is deterministic. An LLM may explain or propose, but it does not
  decide whether enough evidence exists.
- Candidate evaluation is independent from candidate generation. The same
  model response cannot certify its own correctness.
- Existing redaction is preserved. Self-improvement is not a reason to retain
  raw prompts, conversations, memory content, provider responses, or media.
- Existing tool governance remains unchanged. The lab does not call
  `ToolRegistry.run(...)` or bypass
  `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Default development, test, and eval paths remain mock/local/offline. A real
  proposal model is opt-in through `provider_smoke` or `pilot`.
- Version 1 creates review artifacts only. All repository and production
  mutation is out of scope.
- No new dependency is required for version 1.

## Architectural Placement

The lab lives in the `assistant_agent` repository so it can reuse schemas,
redaction rules, provider adapters, skill validation, trace queries, and eval
contracts. It remains outside `AgentGraphRuntime` and all user request entry
paths.

```text
AgentGraphRuntime        pytest/evals
       |                     |
       | redacted trace      | structured result
       +----------+----------+
                  |
                  v
        ImprovementEvidenceCollector
                  |
                  v
        ImprovementOpportunityDetector
                  |
                  v
        ImprovementProposalGenerator
                  |
                  v
        ImprovementCandidateEvaluator
                  |
                  v
        ImprovementRegistry / report
                  |
                  v
              human review
```

Expected source layout:

```text
src/assistant_agent/
  schemas/improvement.py
  services/improvement/
    evidence.py
    detector.py
    proposer.py
    evaluator.py
    registry.py
    report.py

scripts/
  run_improvement_lab.py

tests/
  test_improvement_evidence.py
  test_improvement_detector.py
  test_improvement_proposer.py
  test_improvement_evaluator.py
  test_improvement_registry.py
  test_improvement_cli.py
```

The services use concrete functions and small classes where state is needed.
Version 1 does not introduce an optimizer plugin framework, strategy factory,
background scheduler, or alternate runtime.

## Evidence Contract

### ImprovementEvidence

`ImprovementEvidence` is a prompt-safe fact derived from an existing governed
source.

```python
class ImprovementEvidence(BaseModel):
    schema_version: str = "improvement_evidence_v1"
    evidence_id: str
    source_type: Literal[
        "trajectory",
        "eval_failure",
        "test_failure",
        "metric_anomaly",
    ]
    source_ref: str
    occurred_at: datetime | None = None
    component: str | None = None
    target_hints: list[ImprovementTargetRef] = Field(default_factory=list)
    symptom_code: str
    summary: str
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    severity: Literal["low", "medium", "high"] = "medium"
    redacted: bool = True
```

`ImprovementTargetRef` contains only `target_type` and `target_ref`. Target
hints narrow clustering but do not assert root cause.

Evidence IDs are stable hashes of source type, source reference, symptom code,
and component. They do not hash or encode raw user content.

### Accepted Evidence Sources

Version 1 accepts:

1. `TrajectoryReplayCase` timelines produced by
   `build_redacted_trajectory_replay(...)`;
2. prompt-safe `ContextReport` and trace metric summaries already exposed by
   the observability boundary;
3. structured offline eval failures with case ID, rubric code, component,
   score, threshold, and a bounded redacted failure summary;
4. structured pytest failure summaries produced by an explicit local test
   input adapter;
5. low-cardinality metric anomalies such as repeated retry exhaustion,
   validation rejection, context overflow, or tool budget exhaustion.

Version 1 does not infer user correction evidence from conversation text. A
future explicit feedback event may add that source only after it has its own
prompt-safe schema and retention policy.

### Redaction Boundary

The collector rejects evidence when `redacted` is false or when a safe-field
validator detects:

- raw user text, conversation history, rendered prompts, or system messages;
- memory item content or user profile text;
- provider raw payloads or responses;
- authorization values, cookies, API keys, or secret-like strings;
- raw command output, HTML bodies, base64, data URIs, or media bodies;
- hidden reasoning or chain-of-thought fields.

Trajectory evidence remains intentionally sparse. Operational failures can be
detected from error codes, retry counts, tool names, latency, budget fields,
and terminal status. Semantic skill failures require an eval failure summary or
another explicit prompt-safe rubric result. The design does not weaken trace
redaction to make proposal generation easier.

## Opportunity Contract

`ImprovementOpportunity` represents a repeated or severe pattern that is
eligible for proposal generation.

```python
class ImprovementOpportunity(BaseModel):
    schema_version: str = "improvement_opportunity_v1"
    opportunity_id: str
    target_type: Literal["skill", "runtime", "code"]
    target_ref: str
    evidence_refs: list[str]
    pattern_code: str
    problem_statement: str
    recurrence_count: int
    source_type_count: int
    impact: Literal["low", "medium", "high"]
    confidence: float
    status: Literal[
        "insufficient_evidence",
        "ready_for_proposal",
    ]
    blocked_reasons: list[str] = Field(default_factory=list)
```

### Detection Rules

The detector groups evidence by `(target_type, target_ref, pattern_code)`.
Version 1 uses deterministic rules:

- two independent evidence records are required by default;
- at least one record is sufficient when it is a high-severity eval failure or
  a deterministic test failure;
- duplicate observations derived from the same run, trace, eval case, or test
  invocation count once;
- evidence without a target reference may contribute to a runtime-level
  opportunity but cannot produce a skill or code target by itself;
- a skill semantic opportunity requires at least one eval failure source;
- a code opportunity requires a concrete module or symbol target from an eval
  or test adapter;
- conflicting target hints produce separate opportunities rather than a merged
  diagnosis;
- evidence older than the configured analysis window is ignored but never
  deleted by the detector.

Initial stable pattern codes are:

```text
tool_validation_rejected_repeatedly
tool_retry_exhausted_repeatedly
tool_budget_exhausted
assistant_loop_limit_reached
provider_context_overflow_repeatedly
skill_tool_not_selected_in_eval
skill_tool_selected_incorrectly_in_eval
skill_required_input_missing_in_eval
eval_rubric_regression
deterministic_test_regression
latency_budget_regression
```

Adding a pattern requires a deterministic mapping from accepted evidence. The
proposal model cannot invent new pattern codes during a run.

### Confidence

Confidence is computed locally from recurrence, source diversity, severity,
and target specificity. It is not accepted from model output. The exact
weights are versioned constants and exposed in the report. Confidence measures
evidence strength, not the probability that a proposed fix is correct.

## Candidate Contract

```python
class ImprovementCandidate(BaseModel):
    schema_version: str = "improvement_candidate_v1"
    candidate_id: str
    opportunity_id: str
    target_type: Literal["skill", "runtime", "code"]
    target_ref: str
    current_version: str | None = None
    evidence_refs: list[str]
    failure_pattern: str
    root_cause_hypothesis: str
    proposed_change: str
    affected_locations: list[str] = Field(default_factory=list)
    expected_benefit: str
    patch_preview: str | None = None
    acceptance_criteria: list[str]
    suggested_test_suite_ids: list[str]
    risk_level: Literal["low", "medium", "high"]
    limitations: list[str] = Field(default_factory=list)
    evaluation: CandidateEvaluation
    status: Literal[
        "proposed",
        "evaluation_failed",
        "ready_for_review",
        "rejected",
        "accepted",
    ]
```

`candidate_id` is derived from the opportunity ID, target version, and
canonical proposal payload. Regenerating the same proposal is idempotent.

`accepted` and `rejected` are human decisions. The lab may create only
`proposed`, `evaluation_failed`, or `ready_for_review` records.

### Target-Specific Output

| Target | Version 1 output | Version 1 prohibition |
| --- | --- | --- |
| skill | diagnosis, proposed descriptor changes, acceptance criteria, tests, unified diff preview | writing `SKILL.md`, adding tools, expanding permissions |
| runtime | diagnosis, configuration or algorithm proposal, affected modules, measurable acceptance criteria | changing configuration, prompts, policies, or runtime behavior |
| code | module/symbol location, repair strategy, acceptance criteria, tests | code diff, file edit, branch, commit, or PR |

Skill patch previews are limited to the current skill file. They cannot add
supporting scripts, assets, tools, dependencies, permissions, or files.

## Proposal Generation

### Inputs

The proposal generator receives only:

- one `ImprovementOpportunity`;
- the referenced prompt-safe evidence records;
- a bounded read-only snapshot of the target;
- relevant architecture constraints selected by target type;
- the structured output schema.

For a skill target, the snapshot is the current validated `SKILL.md`. For a
runtime or code target, the snapshot contains prompt-safe module/symbol names,
bounded source excerpts when explicitly allowed by the local developer command,
and architecture rules. It never receives secrets or untracked user data.

### Provider Boundary

Proposal generation uses the existing chat provider adapter boundary with a
dedicated, side-effect-free request mode. It does not expose tools. The output
must validate directly as a proposal payload before it becomes a candidate.

- Tests use scripted/fake chat adapters.
- Default local/offline operation may use a deterministic template proposer
  that produces a diagnosis scaffold without claiming semantic insight.
- A real proposal model requires `provider_smoke` or `pilot` and explicit CLI
  opt-in.
- Provider errors produce a structured failed proposal result and do not retry
  indefinitely.

### Proposal Requirements

A proposal is rejected before evaluation when it:

- cites evidence outside the opportunity;
- changes the target type or target reference;
- claims facts not present in evidence or target snapshot;
- omits measurable acceptance criteria;
- recommends bypassing validator, executor, registry, policy, identity, audit,
  memory policy, provider profile, or redaction boundaries;
- recommends enabling a real provider by default;
- includes arbitrary commands as tests;
- emits a patch for a runtime or code target;
- emits a multi-file or non-skill patch for a skill target.

The generator may produce up to three candidates for one opportunity. Version
1 defaults to one to control cost. Multiple candidates are independently
evaluated; the model does not rank its own outputs.

## Skill Patch Preview

A skill candidate may contain an in-memory unified diff against exactly one
`skills/<skill_id>/SKILL.md` file. The evaluator applies the diff only to an
in-memory string or isolated temporary copy. It never writes through to the
repository skill.

The proposal provider does not generate unified diff syntax. Its structured
payload may contain a complete `replacement_skill_content` value for a skill
target. Local deterministic code validates the replacement content, compares
it with the current skill, and renders `patch_preview` with
`difflib.unified_diff(...)`. `replacement_skill_content` is transient proposal
input and is not included in runtime/code candidates or public trace events.

The candidate skill must pass the existing Skill System v1 contract:

- frontmatter name matches the containing directory;
- the descriptor remains enabled and model-invocable unless the proposal is
  explicitly a disable recommendation, in which case no patch preview is
  produced;
- every governed tool already exists in the current governed tool set;
- every governed tool has a matching `tool:<name>` permission;
- unknown permissions such as `shell:*` are rejected;
- permissions and governed tools cannot expand beyond the current skill;
- required inputs remain compatible with the governed `ToolSpec` schemas;
- the descriptor cannot introduce `run_skill`, raw shell, browser, HTTP, direct
  provider execution, or direct `ToolRegistry.run(...)` behavior;
- the resulting descriptor stays within configured size limits;
- the original skill purpose and target ID remain unchanged.

Version 1 may improve `description`, `Required Inputs`, `When To Use`, `When Not
To Use`, `Safe Examples`, `Runtime Constraints`, `Visibility`, and `Tests`.
Permission or governed-tool expansion requires a separate design and is never
generated automatically.

## Candidate Evaluation

`CandidateEvaluation` uses explicit check results rather than ambiguous
booleans:

```python
CheckStatus = Literal["passed", "failed", "not_run"]

class CandidateCheck(BaseModel):
    check_name: str
    status: CheckStatus
    summary: str
    evidence_refs: list[str] = Field(default_factory=list)

class CandidateEvaluation(BaseModel):
    schema_version: str = "candidate_evaluation_v1"
    checks: list[CandidateCheck]
    regression_suites: list[str]
    blocked_reasons: list[str]
    score: float | None = None
    ready_for_review: bool = False
```

Required checks are:

1. `schema_valid`;
2. `evidence_sufficient`;
3. `evidence_citations_valid`;
4. `target_scope_valid`;
5. `architecture_boundary_passed`;
6. `acceptance_criteria_measurable`;
7. `suggested_tests_allowlisted`;
8. `patch_parse_passed` for skill patches;
9. `skill_manifest_passed` for skill patches;
10. `skill_permission_non_expansion` for skill patches;
11. `semantic_scope_preserved` when a target-specific eval can establish it.

`not_run` is not equivalent to `passed`. A candidate becomes
`ready_for_review` only when all target-required checks pass. Optional semantic
checks may remain `not_run`, but the limitation must be visible and the score
must be capped below a configured threshold.

### Test and Eval Safety

The model may select only `suggested_test_suite_ids` from a local allowlist, for
example:

```text
skill_manifest
skill_tool_contract
tool_catalog
assistant_loop
context_budget
provider_adapter
targeted_pytest:<registered-target>
```

The evaluator maps suite IDs to repository-owned display labels and commands.
It never executes a shell command emitted by the model. Reports render the
resolved commands separately from the candidate contract. Version 1 defaults
to recommending suites without running them. An explicit CLI
`--run-allowlisted-evals` option may run only fixed local/mock/offline suites.
The subprocess environment is rebuilt from a minimal non-secret allowlist and
forces `MULTIMODAL_AGENT_RUNTIME_PROFILE=offline_eval`. Validation completes
before persistence; a failed or errored suite blocks that run's evaluation.

### Scoring

Scoring is deterministic and secondary to hard gates. A candidate with any
blocked reason cannot become review-ready regardless of score. The report
shows the scoring version and contributing fields.

Suggested initial factors are evidence confidence, source diversity,
acceptance-test specificity, target locality, estimated regression surface,
and completed optional evals. Model confidence is not a scoring input.

## Existing Trajectory Gate Relationship

`evaluate_trajectory_improvement_gate(...)` remains a manual-review gate for
the existing `memory` and `skill` trajectory-debug use case. Version 1 does not
broaden `TrajectoryImprovementTarget` to runtime or code and does not weaken
its `auto_apply_allowed=False` contract.

The Improvement Lab reuses `TrajectoryReplayCase` as one evidence source and
adds its own target-neutral candidate evaluator. This avoids turning the
existing diagnostic helper into a general self-modification authority.

## Registry and Reports

### ImprovementRegistry

The local registry records:

- evidence metadata;
- opportunities and their grouping version;
- candidate payloads;
- run-scoped immutable evaluation results, separate from stable candidates;
- allowlisted validation results;
- human decision metadata;
- generation model/profile metadata without raw provider payloads;
- timestamps and schema versions.

The default backend is JSONL under:

```text
.data/improvement_lab/
  evidence.jsonl
  opportunities.jsonl
  candidates.jsonl
  candidate_evaluations.jsonl
  validation_results.jsonl
  decisions.jsonl
  reports/
```

`.data/` remains untracked. Registry writes are append-oriented and atomic per
record. Re-running the same inputs does not duplicate stable evidence,
opportunity, or candidate IDs.

Human decisions are recorded by a separate explicit command. Marking a
candidate accepted does not apply it.

### Markdown Report

Each run produces a review report containing:

1. analysis window and accepted/rejected evidence counts;
2. detected opportunities, including insufficient-evidence cases;
3. candidate summaries grouped by target type;
4. evidence references and recurrence information;
5. proposed changes and affected locations;
6. patch preview for eligible skill candidates;
7. gate results, blocked reasons, score, and limitations;
8. acceptance criteria and allowlisted tests;
9. an explicit statement that no production mutation occurred.

Reports contain references and bounded prompt-safe summaries, not raw source
payloads.

## CLI Design

The first command is explicit and offline:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_improvement_lab.py \
  --trace-id <trace-id> \
  --eval-report <path> \
  --target all \
  --output .data/improvement_lab/reports/
```

Supported selectors:

- one or more `--run-id` or `--trace-id` values;
- one or more structured `--eval-report` inputs;
- one or more structured `--test-report` inputs;
- `--target skill|runtime|code|all`;
- `--skill-id` for a specific skill analysis;
- `--proposal-mode deterministic|provider`;
- `--run-allowlisted-evals` for explicit local validation;
- `--dry-run`, which performs collection and detection but does not persist
  registry records.

`proposal-mode=provider` fails closed unless the runtime profile is
`provider_smoke` or `pilot` and the caller explicitly enabled provider use.
Detecting a configured API key is not authorization.

Version 1 does not include cron, automatic invocation after every run, a web
endpoint, or an assistant-callable `improve_self` tool.

## Data Flow

One lab invocation follows this sequence:

1. Resolve explicit source selectors and target filters.
2. Query existing stores through read-only adapters.
3. Convert sources into prompt-safe `ImprovementEvidence`.
4. Reject unsafe or invalid evidence and record reason codes.
5. Deduplicate evidence by stable ID.
6. Group evidence into deterministic opportunities.
7. Preserve insufficient-evidence opportunities for visibility, but do not
   send them to the proposal generator.
8. Build a bounded target snapshot for each ready opportunity.
9. Generate one or more structured proposal payloads.
10. Validate proposal provenance and target scope.
11. Evaluate each candidate independently.
12. Persist evidence, opportunities, candidates, and evaluations unless
    `--dry-run` was selected.
13. Render the Markdown report.
14. Exit non-zero only for lab infrastructure failure, not because no useful
    opportunity was found.

## Error Handling

Errors are structured and scoped so one bad source or candidate does not erase
the rest of a run.

Stable error categories include:

```text
evidence_source_not_found
evidence_schema_invalid
evidence_not_redacted
evidence_unsafe_field
evidence_target_unresolved
opportunity_insufficient_evidence
target_snapshot_unavailable
proposal_provider_unavailable
proposal_provider_failed
proposal_schema_invalid
proposal_unsupported_claim
proposal_scope_expansion
patch_invalid
candidate_gate_failed
registry_write_failed
report_write_failed
```

Rules:

- unsafe evidence is rejected, never summarized by a provider;
- a missing source is reported while other explicit sources continue;
- provider failure does not fall back to a real alternate provider;
- invalid model output may receive one schema-repair attempt using the same
  adapter, then becomes a failed proposal result;
- a failed candidate remains visible with blocked reasons;
- registry failure prevents the run from claiming persistence success;
- report failure returns a non-zero exit after registry status is recorded when
  possible;
- no exception path applies a candidate or changes a target file.

## Observability

The lab emits local prompt-safe lifecycle records separate from production
assistant traces:

```text
improvement.run.started
improvement.evidence.accepted
improvement.evidence.rejected
improvement.opportunity.detected
improvement.proposal.started
improvement.proposal.finished
improvement.proposal.failed
improvement.candidate.evaluated
improvement.report.written
improvement.run.completed
improvement.run.failed
```

Safe attributes include IDs, target type, pattern code, counts, status, gate
names, error codes, latency, provider/model labels, and token counts. Events do
not contain evidence summaries, target source excerpts, generated patch text,
prompts, or raw provider output.

## Security and Trust Boundaries

- Evidence and target source are data, not instructions. Prompt compilation
  labels them as untrusted diagnostic material.
- The proposal provider has no tools and no filesystem access.
- Target paths are resolved from repository-owned indexes, not model output.
- Patch paths are parsed and restricted to one expected skill file.
- The model cannot select arbitrary commands, environment variables, provider
  profiles, or test paths.
- Untracked files outside configured evidence/report roots are not scanned.
- Symlinks and paths outside the repository root are rejected for target
  snapshots and patch previews.
- Registry and reports never persist raw provider responses.
- Human acceptance records intent only; a future application workflow requires
  a separate approved design.

## Test Design

Implementation follows TDD. Required test groups are:

### Evidence

1. Safe trajectory events convert into stable evidence records.
2. Duplicate source records produce one evidence ID.
3. Raw conversation, memory, provider, secret, command, and media fields are
   rejected.
4. An operational trajectory can create a runtime symptom but cannot claim a
   semantic skill failure.
5. A structured eval failure can create a skill semantic symptom without
   exposing eval prompt or expected-answer text.

### Detection

6. One ordinary observation remains insufficient.
7. Two independent observations create a ready opportunity.
8. Repeated events from one run count once.
9. One high-severity eval or deterministic test failure is eligible.
10. Skill semantic patterns require eval evidence.
11. Code targets require a concrete module or symbol.
12. Confidence is deterministic and versioned.

### Proposal Generation

13. Scripted provider output validates into a candidate.
14. Evidence references outside the opportunity are rejected.
15. Unsupported facts and governance bypass recommendations are rejected.
16. Provider mode fails closed outside `provider_smoke` and `pilot`.
17. Provider output receives at most one schema-repair attempt.
18. No tools are exposed to the proposal provider.

### Skill Patch Preview

19. A single-file descriptor-only patch parses in memory.
20. A multi-file patch is rejected.
21. Governed-tool or permission expansion is rejected.
22. Missing `tool:<name>` permission and unknown permissions are rejected.
23. A valid change to usage guidance passes existing skill validation.
24. No test writes the candidate into the repository skill directory.

### Candidate Evaluation

25. Hard gate failure prevents `ready_for_review` regardless of score.
26. `not_run` is not treated as `passed`.
27. Runtime and code patch payloads are rejected.
28. Suggested test commands outside the suite allowlist are rejected.
29. Fixed allowlisted evals run only with explicit CLI authorization.

### Registry, Reports, and CLI

30. Registry writes are append-oriented and idempotent by stable ID.
31. Human acceptance never applies a candidate.
32. Reports contain required evidence and gate sections without unsafe data.
33. `--dry-run` writes no registry records.
34. One invalid source does not discard valid-source candidates.
35. Infrastructure failures return non-zero; no-opportunity runs return zero.
36. Existing trajectory-debug, context-report, skill-system, provider-profile,
    and tool-governance tests remain unchanged or gain only additive coverage.

## Delivery Phases

### Phase 1: Contracts and Safe Evidence

- Add evidence, opportunity, candidate, evaluation, and run-report schemas.
- Add adapters for redacted trajectory replay and structured eval/test results.
- Add redaction and deduplication tests.

### Phase 2: Deterministic Detection

- Add stable pattern codes, grouping, severity, confidence, and eligibility
  rules.
- Produce dry-run opportunity reports without proposal generation.

### Phase 3: Structured Proposals

- Add deterministic scaffold proposer and scripted-provider tests.
- Add opt-in provider proposal mode with structured output validation.
- Generate runtime/code recommendations and skill diff previews.

### Phase 4: Evaluation and Registry

- Add architecture gates, skill patch validation, test-suite allowlist, scoring,
  JSONL registry, and Markdown reports.
- Integrate explicit local/mock/offline eval execution.

### Phase 5: Acceptance Hardening

- Run targeted tests, fast tests, full tests, and offline evals.
- Add representative fixtures for recurring tool, context, skill, runtime, and
  code failure patterns.
- Update authoritative observability, context, and tool-calling documents with
  the implemented boundaries.

Each phase may be implemented and validated independently. No phase introduces
automatic application.

## Alternatives Considered

### Reflection Inside AgentGraphRuntime

Adding a post-run reflection node would have direct access to state but would
couple engineering optimization to user latency, token budgets, cancellation,
and production behavior. It would also let the same runtime generate and judge
its own proposal. This design rejects that approach.

### Background Improvement Service

A continuously scheduled service could discover patterns sooner, but it adds
retention, concurrency, cost, and operational controls before the proposal
quality is known. Version 1 uses an explicit offline command. A scheduler may
consume the same contracts later.

### DSPy/GEPA Evolution From the Start

Evolutionary search is useful once stable datasets and evaluators exist. In
version 1 it would add dependencies and cost while hiding whether weak results
come from evidence, metrics, mutation, or evaluation. The first implementation
builds the evidence and evaluation substrate. A future optimizer can generate
multiple candidates behind the same proposal contract.

## Out of Scope

- Automatic skill, runtime, prompt, policy, tool, provider, memory, or code
  mutation.
- Automatic branches, commits, pull requests, merges, deployment, restart, or
  rollback.
- Online reinforcement learning, model fine-tuning, or private-data training.
- Raw conversation mining or hidden-reasoning collection.
- A user-facing self-improvement API or assistant-callable tool.
- Continuous background scheduling.
- Arbitrary shell execution proposed by a model.
- Skill permission or governed-tool expansion.
- Runtime or code patch generation.
- Replacing `AgentGraphRuntime`, provider-native tool calling, validator,
  executor, registry, memory policy, or current skill loader.

## Acceptance Criteria

- An explicit offline command can consume redacted trace and structured
  eval/test evidence and produce a review report.
- Evidence containing raw or unsafe fields is rejected before proposal model
  access.
- Repeated operational failures produce deterministic runtime opportunities.
- Skill semantic opportunities require prompt-safe eval evidence.
- Every candidate cites only evidence from its source opportunity.
- Runtime and code candidates contain no patch.
- Skill candidates may contain only a validated, permission-non-expanding,
  single-file diff preview.
- Candidate generation is mock/scripted by default and real-provider use is
  explicitly profile-gated.
- Hard evaluation failures prevent review-ready status regardless of score.
- Suggested tests come only from a repository-owned allowlist.
- Registry and reports are local, redacted, versioned, and idempotent.
- Human acceptance records a decision but never applies a candidate.
- Normal CLI, API, Gateway, WebSocket, realtime, mock/offline, and provider-native
  assistant paths do not invoke the Improvement Lab.
- No implementation path bypasses existing tool, memory, context, identity,
  policy, audit, or provider-profile boundaries.

## Future Extensions

Only after version 1 proposal quality is measured:

1. add explicit prompt-safe user correction events;
2. schedule periodic read-only discovery;
3. compare multiple proposal candidates with fixed offline evaluators;
4. introduce an optional optimizer such as GEPA behind the proposal contract;
5. generate code patch previews in isolated worktrees;
6. create human-reviewed PRs;
7. define low-risk canary promotion and rollback in a separate design.

None of these extensions changes the version 1 non-mutation guarantee.
