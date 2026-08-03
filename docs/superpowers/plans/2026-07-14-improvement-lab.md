# Improvement Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline, non-mutating engineering loop that turns redacted trace and structured eval/test failures into evaluated skill, runtime, and code improvement proposals for human review.

**Architecture:** Add focused improvement schemas and services outside `AgentGraphRuntime`. Deterministic collectors and detectors establish evidence eligibility; a no-tool proposal adapter creates structured candidates; independent local gates validate scope, skill replacements, and test-suite IDs before JSONL persistence and Markdown reporting.

**Tech Stack:** Python 3.11, Pydantic v2, existing `ChatAdapter`, existing trace and skill-loader contracts, standard-library `hashlib`, `json`, `difflib`, `tempfile`, `pathlib`, `argparse`, pytest.

## Global Constraints

- Do not add dependencies or make network calls in tests.
- Keep normal CLI/API/Gateway/assistant-loop paths unchanged.
- Do not weaken trace redaction or persist raw prompts, conversations, memory, provider payloads, command output, or media.
- Do not call tools or expose tools to the proposal provider.
- Do not write candidate changes into `src/**`, `skills/**`, configuration, git branches, commits, or pull requests.
- Only skill targets may produce a patch preview, generated locally with `difflib.unified_diff` from validated replacement content.
- Runtime and code targets produce recommendations only.
- Real proposal providers require `provider_smoke` or `pilot`; tests use scripted adapters.
- Model-selected tests are symbolic suite IDs resolved through a repository-owned allowlist.
- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest commands.

---

### Task 1: Improvement Schemas

**Files:**
- Create: `src/assistant_agent/schemas/improvement.py`
- Create: `tests/test_improvement_schemas.py`

**Interfaces:**
- Produces: `ImprovementTargetRef`, `ImprovementEvidence`, `ImprovementOpportunity`, `CandidateCheck`, `CandidateEvaluation`, `ImprovementCandidate`, `ImprovementDecision`, `ImprovementRunReport`, and the target/source/status literal aliases.
- Consumes: Pydantic `BaseModel`, `Field`, `model_validator`; JSON-safe values use `JsonValue` from `pydantic`.

- [ ] **Step 1: Write failing schema tests**

Cover minimum field constraints, evidence redaction default, opportunity confidence range, candidate target/patch invariants, human-only terminal statuses, and `not_run` check status. Include tests proving runtime/code candidates reject `patch_preview` and skill candidates accept it.

- [ ] **Step 2: Run the schema tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_schemas.py -q
```

Expected: collection fails because `assistant_agent.schemas.improvement` does not exist.

- [ ] **Step 3: Implement the minimal Pydantic contracts**

Use `Literal` schema versions and statuses. Enforce:

```python
@model_validator(mode="after")
def validate_patch_scope(self) -> "ImprovementCandidate":
    if self.target_type != "skill" and self.patch_preview is not None:
        raise ValueError("only skill candidates may contain patch_preview")
    return self
```

Keep `accepted` and `rejected` available for registry decision records, while proposal services create only pre-review statuses.

- [ ] **Step 4: Run schema tests and verify GREEN**

Run the Task 1 command. Expected: all tests pass.

### Task 2: Safe Evidence Collection

**Files:**
- Create: `src/assistant_agent/services/improvement/__init__.py`
- Create: `src/assistant_agent/services/improvement/evidence.py`
- Create: `tests/test_improvement_evidence.py`

**Interfaces:**
- Consumes: `TrajectoryReplayCase`, `ImprovementEvidence`, structured JSON mappings.
- Produces:

```python
def collect_trajectory_evidence(replay: TrajectoryReplayCase) -> list[ImprovementEvidence]
def load_structured_evidence(path: Path, *, source_type: Literal["eval_failure", "test_failure"]) -> list[ImprovementEvidence]
def validate_evidence_safety(evidence: ImprovementEvidence) -> list[str]
def deduplicate_evidence(items: Iterable[ImprovementEvidence]) -> list[ImprovementEvidence]
```

- [ ] **Step 1: Write failing trajectory and structured-input tests**

Test stable IDs, duplicate removal, retry/overflow/validation/loop-limit symptom mapping, single source-ref identity, invalid JSON shape, unsafe key/value rejection, and absence of raw timeline content. A semantic skill symptom loaded from eval JSON must require a skill target hint.

Use a structured input shaped as:

```json
{
  "schema_version": "improvement_source_records_v1",
  "records": [
    {
      "source_ref": "eval:skill_search:case_1",
      "component": "skills/realtime_web_search/SKILL.md",
      "target_type": "skill",
      "target_ref": "realtime_web_search",
      "symptom_code": "skill_tool_not_selected_in_eval",
      "summary": "Governed search tool was not selected in the offline eval.",
      "severity": "high",
      "attributes": {"rubric_code": "expected_tool_missing", "score": 0.0, "threshold": 1.0}
    }
  ]
}
```

- [ ] **Step 2: Run evidence tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_evidence.py -q
```

Expected: import failure for the missing evidence service.

- [ ] **Step 3: Implement safe collectors**

Use an explicit symptom map from trajectory error/canonical event values to stable pattern codes. Generate IDs with SHA-256 over canonical JSON containing source type, source ref, component, and symptom code. Reject non-redacted replay cases before inspecting timelines. Recursively reject keys containing secret/raw/prompt/conversation/memory/body/media/authorization markers and strings containing secret/base64/data-URI markers.

- [ ] **Step 4: Run evidence tests and verify GREEN**

Run the Task 2 command. Expected: all tests pass.

### Task 3: Deterministic Opportunity Detection

**Files:**
- Create: `src/assistant_agent/services/improvement/detector.py`
- Create: `tests/test_improvement_detector.py`

**Interfaces:**
- Consumes: `Sequence[ImprovementEvidence]`, optional target filter.
- Produces:

```python
def detect_opportunities(
    evidence: Sequence[ImprovementEvidence],
    *,
    target_type: ImprovementTargetType | None = None,
) -> list[ImprovementOpportunity]
```

- [ ] **Step 1: Write failing eligibility and confidence tests**

Test one ordinary observation is insufficient; two independent sources are ready; two records from one source count once; one high-severity eval/test failure is ready; semantic skill patterns require eval evidence; code targets require `module` or `symbol` attribute; target filtering works; IDs and confidence are deterministic.

- [ ] **Step 2: Run detector tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_detector.py -q
```

Expected: import failure for the missing detector.

- [ ] **Step 3: Implement grouping and confidence**

Group by `(target_type, target_ref, symptom_code)`. Count unique `source_ref` values. Use an explicit confidence formula capped at `1.0`, for example base `0.35`, `+0.2` for a second independent source, `+0.1` for source-type diversity, `+0.2` for high severity, and `+0.15` for a concrete target. Record formula version `opportunity_confidence_v1` in `blocked_reasons` or a dedicated schema field.

- [ ] **Step 4: Run detector tests and verify GREEN**

Run the Task 3 command. Expected: all tests pass.

### Task 4: Structured Proposal Generation

**Files:**
- Create: `src/assistant_agent/services/improvement/proposer.py`
- Create: `tests/test_improvement_proposer.py`

**Interfaces:**
- Consumes: ready opportunity, referenced evidence, repository root, optional `ChatAdapter`, `RuntimeProfile`, and mode.
- Produces:

```python
class ProposalResult(BaseModel):
    candidate: ImprovementCandidate | None
    replacement_skill_content: str | None = None
    error_code: str | None = None
    repair_attempted: bool = False

def generate_proposal(
    opportunity: ImprovementOpportunity,
    evidence: Sequence[ImprovementEvidence],
    *,
    repo_root: Path,
    mode: Literal["deterministic", "provider"] = "deterministic",
    adapter: ChatAdapter | None = None,
    runtime_profile: RuntimeProfile | None = None,
) -> ProposalResult
```

- [ ] **Step 1: Write failing deterministic and scripted-provider tests**

Verify deterministic mode produces a concrete recommendation scaffold; provider mode is blocked in local/offline profiles; provider requests contain no tools; response JSON is schema-validated; evidence refs outside the opportunity fail; runtime/code replacement content fails; architecture-bypass language fails; invalid JSON gets one repair request and no more; provider errors become structured failures.

- [ ] **Step 2: Run proposer tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_proposer.py -q
```

Expected: import failure for the missing proposer.

- [ ] **Step 3: Implement proposal modes and validation**

Build a `ChatRequest` with `tools=[]`, bounded evidence JSON, target metadata, architecture constraints, and a JSON-only response instruction. Provider mode requires `runtime_profile.allows_real_providers` and a caller-supplied adapter. Parse `ChatResult.response_text`; make one repair request after JSON/schema failure. Do not create a provider adapter inside the service. Deterministic mode derives affected locations, acceptance criteria, and suite IDs from target type and pattern code without claiming a proven root cause.

- [ ] **Step 4: Run proposer tests and verify GREEN**

Run the Task 4 command. Expected: all tests pass.

### Task 5: Independent Candidate Evaluation and Skill Diff Preview

**Files:**
- Create: `src/assistant_agent/services/improvement/evaluator.py`
- Create: `tests/test_improvement_evaluator.py`

**Interfaces:**
- Consumes: `ProposalResult`, opportunity, evidence, repository root.
- Produces:

```python
TEST_SUITE_COMMANDS: dict[str, tuple[str, ...]]

def evaluate_candidate(
    result: ProposalResult,
    opportunity: ImprovementOpportunity,
    evidence: Sequence[ImprovementEvidence],
    *,
    repo_root: Path,
) -> ImprovementCandidate | None

def resolved_test_commands(candidate: ImprovementCandidate) -> list[str]
```

- [ ] **Step 1: Write failing hard-gate tests**

Test evidence citation validation, target equality, measurable acceptance criteria, suite-ID allowlist, runtime/code patch prohibition, and hard-gate status. For skills, test local unified diff generation, single expected path, unchanged governed tools and permissions, valid loader result, invalid permission rejection, and no repository write.

- [ ] **Step 2: Run evaluator tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_evaluator.py -q
```

Expected: import failure for the missing evaluator.

- [ ] **Step 3: Implement local gates**

For skill replacement content, load the current descriptor, validate the replacement through an isolated `TemporaryDirectory` using `load_repo_skill_descriptors`, compare governed tools and permission sets, then render a local unified diff with repository-relative `fromfile` and `tofile`. Never apply the diff. Use exact forbidden-boundary phrase checks as a conservative first gate. A failed required check sets `evaluation_failed`; all required checks passed sets `ready_for_review`.

- [ ] **Step 4: Run evaluator tests and verify GREEN**

Run the Task 5 command. Expected: all tests pass.

### Task 6: JSONL Registry and Markdown Reports

**Files:**
- Create: `src/assistant_agent/services/improvement/registry.py`
- Create: `src/assistant_agent/services/improvement/report.py`
- Create: `tests/test_improvement_registry.py`
- Create: `tests/test_improvement_report.py`

**Interfaces:**
- Produces:

```python
class JsonlImprovementRegistry:
    def append_evidence(self, items: Sequence[ImprovementEvidence]) -> int: ...
    def append_opportunities(self, items: Sequence[ImprovementOpportunity]) -> int: ...
    def append_candidates(self, items: Sequence[ImprovementCandidate]) -> int: ...
    def record_decision(self, decision: ImprovementDecision) -> bool: ...

def render_improvement_report(report: ImprovementRunReport) -> str
```

- [ ] **Step 1: Write failing persistence and redaction tests**

Verify stable-ID idempotency across registry instances, separate JSONL files, accepted/rejected decisions do not mutate candidate records, malformed existing lines are skipped with issues, report sections are present, patch previews render in fenced diff blocks, and unsafe payload strings never appear.

- [ ] **Step 2: Run registry/report tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_registry.py tests/test_improvement_report.py -q
```

Expected: import failures for missing registry/report services.

- [ ] **Step 3: Implement append-oriented registry and renderer**

Use atomic append per record with UTF-8 JSON lines. Load only IDs/status needed for deduplication. Keep decisions in `decisions.jsonl`; never rewrite candidate files. Report counts, insufficient evidence, candidate gates, limitations, suite IDs, resolved fixed commands, and a final non-mutation statement.

- [ ] **Step 4: Run registry/report tests and verify GREEN**

Run the Task 6 command. Expected: all tests pass.

### Task 7: Offline Orchestration CLI and Authority Documentation

**Files:**
- Create: `src/assistant_agent/services/improvement/lab.py`
- Create: `scripts/run_improvement_lab.py`
- Create: `tests/test_improvement_lab.py`
- Create: `tests/test_improvement_cli.py`
- Modify: `docs/observability-harness.md`
- Modify: `docs/context_engineering_status.md`
- Modify: `docs/tool-calling-architecture.md`

**Interfaces:**
- Produces:

```python
def run_improvement_lab(
    *,
    trace_store: TraceStore | None,
    run_ids: Sequence[str],
    trace_ids: Sequence[str],
    eval_paths: Sequence[Path],
    test_paths: Sequence[Path],
    target_type: ImprovementTargetType | None,
    skill_id: str | None,
    repo_root: Path,
    registry_root: Path,
    persist: bool,
    proposal_mode: Literal["deterministic", "provider"],
    adapter: ChatAdapter | None = None,
    runtime_profile: RuntimeProfile | None = None,
) -> ImprovementRunReport
```

- [ ] **Step 1: Write failing orchestration and CLI tests**

Test an end-to-end deterministic skill opportunity from structured eval JSON; two trajectory runs becoming one runtime opportunity; insufficient evidence creates no proposal; one invalid source does not discard a valid source; dry-run creates no registry; persisted run writes registry/report; missing explicit IDs return a source issue; CLI exits zero for no opportunity and non-zero for infrastructure/report failure; CLI help exposes only designed options.

- [ ] **Step 2: Run lab/CLI tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_improvement_lab.py tests/test_improvement_cli.py -q
```

Expected: import or script-not-found failure.

- [ ] **Step 3: Implement orchestration and CLI**

The CLI configures `JsonlTraceStore`, parses structured sources, invokes the lab, writes the Markdown report under the selected output directory, and prints a small JSON summary. Provider mode uses existing `ProviderConfig.from_env()` and `create_chat_adapter(...)` only after explicit mode/profile checks. Do not add a server route, runtime hook, scheduler, or assistant tool.

- [ ] **Step 4: Update authority docs**

Document the implemented offline boundary, evidence redaction, non-mutation guarantee, provider profile gate, and relationship to existing trajectory debug. Do not present future optimization or auto-application as implemented.

- [ ] **Step 5: Run targeted tests and verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_improvement_schemas.py \
  tests/test_improvement_evidence.py \
  tests/test_improvement_detector.py \
  tests/test_improvement_proposer.py \
  tests/test_improvement_evaluator.py \
  tests/test_improvement_registry.py \
  tests/test_improvement_report.py \
  tests/test_improvement_lab.py \
  tests/test_improvement_cli.py -q
```

Expected: all tests pass.

### Task 8: Regression and Acceptance Verification

**Files:**
- Verify all files created or modified by Tasks 1-7.

**Interfaces:**
- Consumes the completed Improvement Lab.
- Produces fresh verification evidence only; no new behavior.

- [ ] **Step 1: Run environment validation**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
```

Expected: exit 0.

- [ ] **Step 2: Run focused neighboring regression tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_phase5_trajectory_debug_gate.py \
  tests/test_skill_loader.py \
  tests/test_runtime_profile.py \
  tests/test_trace_redaction.py \
  tests/test_trace_metrics.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: all selected tests pass.

- [ ] **Step 4: Run full suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: all tests pass. If unrelated pre-existing failures exist, capture exact failing tests and confirm targeted Improvement Lab tests still pass.

- [ ] **Step 5: Run offline evals**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py
```

Expected: exit 0 with no regression in the reported offline suites.

- [ ] **Step 6: Run diff validation and inspect scope**

```bash
git diff --check -- AGENTS.md docs src tests scripts .codex/skills
git status --short
```

Expected: no whitespace errors; only the design, plan, Improvement Lab implementation, tests, script, and relevant authority docs are modified.

## Plan Self-Review

- Every version 1 design component maps to Tasks 1-7.
- The plan explicitly preserves the existing trajectory gate rather than broadening it.
- Type names and signatures are consistent across tasks.
- Proposal generation and evaluation are separate.
- Skill diff syntax is generated locally, never by the model.
- No task introduces automatic application, new dependencies, background scheduling, API routes, or runtime hooks.
- TDD red/green commands are specified for every production component.
