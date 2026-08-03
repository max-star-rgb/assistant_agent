# Tool Qualification and Identity Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not dispatch subagents unless the user explicitly authorizes delegation.

**Goal:** Replace request-text tool routing with deterministic qualification plus identity recall while preserving the validator/executor governance chain.

**Architecture:** `select_prompt_tool_specs()` will call a pure qualification function followed by a pure identity-recall function. `RunToolSet` records registered, qualified, exposed, and executable sets; current provider-native behavior exposes and authorizes every qualified tool, while risk remains an execution concern.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, existing provider-native assistant loop.

## Global Constraints

- The LLM is the only task reasoner; production qualification must not inspect `request.text`.
- User text must not activate tools, toolsets, or skills.
- Risk changes runtime gates, not exposure of an otherwise qualified tool.
- Recall is a pure identity function in this phase; do not add a strategy interface, embedding index, or meta-tool.
- Keep real providers opt-in and preserve mock/local/offline defaults.
- Keep all execution behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Do not install dependencies or modify unrelated dirty-worktree files.
- Do not create intermediate commits in the existing dirty worktree; report the final diff for user-controlled integration.

---

## File Map

- `src/assistant_agent/schemas/tools.py`: `RunToolSet` field names and subset invariants.
- `src/assistant_agent/services/context/tool_catalog.py`: qualification and identity recall.
- `src/assistant_agent/services/context/capability_catalog.py`: explicit skill descriptor activation.
- `src/assistant_agent/services/context/builder.py`: qualified tool and explicit skill data flow.
- `src/assistant_agent/agent/assistant_loop_nodes.py`: remove mock-only executable widening.
- `tests/test_tool_catalog.py`: qualification, identity recall, risk exposure, and set contract tests.
- `tests/test_phase3_skill_system_gate.py`: explicit skill activation tests.
- `tests/test_native_tool_call_handoff.py`: provider sees all qualified tools and LLM may choose among them.
- Other affected tests: update old semantic prompt-subset and field-name expectations.
- `docs/tool-calling-architecture.md` and `docs/context_engineering_status.md`: authoritative behavior.

---

### Task 1: RunToolSet Contract, Qualification, and Identity Recall

**Files:**
- Modify: `src/assistant_agent/schemas/tools.py`
- Modify: `src/assistant_agent/services/context/tool_catalog.py`
- Modify: `tests/test_tool_catalog.py`

**Interfaces:**
- Produces: `RunToolSet.qualified_tool_names: list[str]`.
- Produces: `qualify_tool_specs(request, tool_specs, *, skill_catalog=None) -> ToolQualificationSelection`.
- Produces: `recall_qualified_tool_specs(request, qualified_tool_specs) -> list[ToolSpec]`.
- Produces: `ToolCatalogSelection.qualified_tool_specs` and `active_skill_ids`.

- [x] **Step 1: Write failing tests for text-independent identity recall**

Replace semantic subset assertions with tests equivalent to:

```python
def test_tool_catalog_exposes_all_qualified_tools_independent_of_request_text() -> None:
    specs = create_default_registry().list_specs()
    requests = [
        UserRequest(user_id="u1", session_id="s1", text="帮我找耳机"),
        UserRequest(user_id="u1", session_id="s1", text="写一段文案"),
        UserRequest(user_id="u1", session_id="s1", text="Momentum 4 值不值得入"),
    ]

    selections = [select_prompt_tool_specs(request, specs) for request in requests]

    expected = [spec.name for spec in specs]
    assert [[spec.name for spec in item.qualified_tool_specs] for item in selections] == [
        expected,
        expected,
        expected,
    ]
    assert [[spec.name for spec in item.prompt_tool_specs] for item in selections] == [
        expected,
        expected,
        expected,
    ]
    assert all(item.run_tool_set.executable_tool_names == expected for item in selections)
    assert all(item.summary.selection_reasons == ["recall_identity"] for item in selections)


def test_identity_recall_preserves_qualified_tool_order() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="arbitrary text")
    specs = [ToolSpec(name="third"), ToolSpec(name="first"), ToolSpec(name="second")]

    recalled = recall_qualified_tool_specs(request, specs)

    assert recalled == specs
    assert recalled is not specs
```

- [x] **Step 2: Write failing tests for qualification and risk exposure**

```python
def test_qualification_keeps_all_risk_levels_visible() -> None:
    specs = [
        ToolSpec(name="read", policy=ToolPolicyMetadata(risk="local_read")),
        ToolSpec(name="artifact", policy=ToolPolicyMetadata(risk="transactional")),
        ToolSpec(name="write", policy=ToolPolicyMetadata(risk="external_write")),
    ]
    request = UserRequest(user_id="u1", session_id="s1", text="does not classify tools")

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.qualified_tool_names == ["read", "artifact", "write"]
    assert selection.run_tool_set.exposed_tool_names == ["read", "artifact", "write"]
    assert selection.run_tool_set.executable_tool_names == ["read", "artifact", "write"]


def test_run_tool_set_rejects_exposed_tool_outside_qualified_set() -> None:
    with pytest.raises(ValidationError, match="exposed_tool_names"):
        RunToolSet(
            registered_tool_names=["registered"],
            qualified_tool_names=["registered"],
            exposed_tool_names=["hidden"],
        )
```

Update the missing-env and disabled-tool tests to assert `qualified_tool_specs` and `qualified_tool_names` instead of `available_*`.

- [x] **Step 3: Run the new tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_tool_catalog.py::test_tool_catalog_exposes_all_qualified_tools_independent_of_request_text \
  tests/test_tool_catalog.py::test_identity_recall_preserves_qualified_tool_order \
  tests/test_tool_catalog.py::test_qualification_keeps_all_risk_levels_visible \
  tests/test_tool_catalog.py::test_run_tool_set_rejects_exposed_tool_outside_qualified_set
```

Expected: FAIL because `qualified_tool_specs`, `qualified_tool_names`, and `recall_qualified_tool_specs` do not yet exist and current semantic routing returns subsets.

- [x] **Step 4: Implement the minimal RunToolSet contract**

Use a Pydantic `model_validator(mode="after")`:

```python
class RunToolSet(BaseModel):
    schema_version: Literal["run_tool_set_v1"] = "run_tool_set_v1"
    registered_tool_names: list[str] = Field(default_factory=list)
    qualified_tool_names: list[str] = Field(default_factory=list)
    exposed_tool_names: list[str] = Field(default_factory=list)
    executable_tool_names: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    excluded_reasons: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_tool_sets(self) -> "RunToolSet":
        registered = set(self.registered_tool_names)
        qualified = set(self.qualified_tool_names)
        for field_name, names, allowed in (
            ("qualified_tool_names", qualified, registered),
            ("exposed_tool_names", set(self.exposed_tool_names), qualified),
            ("executable_tool_names", set(self.executable_tool_names), qualified),
        ):
            unknown = sorted(names - allowed)
            if unknown:
                raise ValueError(f"{field_name} contains tools outside its allowed set: {unknown}")
        return self
```

- [x] **Step 5: Implement qualification and identity recall**

Keep the public `select_prompt_tool_specs()` entry and introduce small data/function boundaries:

```python
@dataclass(frozen=True)
class ToolQualificationSelection:
    qualified_tool_specs: list[ToolSpec]
    active_skill_ids: list[str]
    excluded_reasons: dict[str, list[str]]


def recall_qualified_tool_specs(
    request: UserRequest,
    qualified_tool_specs: list[ToolSpec],
) -> list[ToolSpec]:
    del request
    return list(qualified_tool_specs)
```

`qualify_tool_specs()` must apply only `requires_env`, `enabled_by_default`, explicit enabled tools/toolsets, and explicit valid skill permissions. Remove request-text matching, `_has_*_intent()` selection, `_has_substantive_text_task()`, `_skill_matches_request()`, keyword constants, fallback, and `not_selected_for_prompt` behavior.

Construct the final selection as:

```python
recalled_specs = recall_qualified_tool_specs(request, qualification.qualified_tool_specs)
names = [spec.name for spec in recalled_specs]
run_tool_set = RunToolSet(
    registered_tool_names=[spec.name for spec in tool_specs],
    qualified_tool_names=[spec.name for spec in qualification.qualified_tool_specs],
    exposed_tool_names=names,
    executable_tool_names=names,
    selection_reasons=["recall_identity"],
    excluded_reasons=qualification.excluded_reasons,
)
```

- [x] **Step 6: Run Task 1 tests and verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_tool_catalog.py
```

Expected: all `test_tool_catalog.py` tests pass after obsolete semantic-subset cases are replaced with qualification/identity cases.

---

### Task 2: Explicit Skill Capability Exposure

**Files:**
- Modify: `src/assistant_agent/services/context/capability_catalog.py`
- Modify: `src/assistant_agent/services/context/builder.py`
- Modify: `tests/test_phase3_skill_system_gate.py`
- Modify: `tests/test_tool_catalog.py`

**Interfaces:**
- Consumes: `ToolCatalogSelection.active_skill_ids` and `qualified_tool_specs` from Task 1.
- Produces: `select_tool_capability_descriptors(..., active_skill_ids: set[str])`.

- [x] **Step 1: Write failing tests for explicit-only skill activation**

```python
def test_request_text_does_not_activate_skill_capability(tmp_path: Path) -> None:
    _write_skill(tmp_path, "tagged_search", """
---
name: tagged_search
description: Search guidance.
---
## Governed Tools
- web_search
## Permissions
- tool:web_search
## Visibility
- tags: latest-news
""")
    catalog = load_repo_skill_descriptors(tmp_path)
    request = UserRequest(user_id="u1", session_id="s1", text="latest-news")

    selection = select_prompt_tool_specs(
        request,
        [ToolSpec(name="web_search")],
        skill_catalog=catalog,
    )

    assert selection.active_skill_ids == []


def test_explicit_enabled_skill_activates_capability_and_skill_only_tool(tmp_path: Path) -> None:
    _write_skill(tmp_path, "private_search", """
---
name: private_search
description: Private search guidance.
---
## Governed Tools
- private.lookup
## Permissions
- tool:private.lookup
""")
    catalog = load_repo_skill_descriptors(tmp_path)
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="unclassified text",
        metadata={"tool_visibility": {"enabled_skills": ["private_search"]}},
    )
    spec = ToolSpec(
        name="private.lookup",
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(enabled_by_default=False, skill_only=True)
        ),
    )

    selection = select_prompt_tool_specs(request, [spec], skill_catalog=catalog)

    assert selection.active_skill_ids == ["private_search"]
    assert selection.run_tool_set.qualified_tool_names == ["private.lookup"]
```

- [x] **Step 2: Run explicit skill tests and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_phase3_skill_system_gate.py::test_request_text_does_not_activate_skill_capability \
  tests/test_phase3_skill_system_gate.py::test_explicit_enabled_skill_activates_capability_and_skill_only_tool
```

Expected: FAIL because current skill activation matches selected tools/text and capability exposure does not require explicit activation.

- [x] **Step 3: Implement explicit capability filtering**

Add `active_skill_ids: set[str]` to `select_tool_capability_descriptors()`. Before adding a descriptor, require:

```python
if descriptor.name not in active_skill_ids:
    report.skipped.append(
        SkillExposureSkip(skill_id=descriptor.name, reason="skill_not_explicitly_enabled")
    )
    reasons.append(
        f"capability_catalog_skipped:{descriptor.name}:skill_not_explicitly_enabled"
    )
    continue
```

In `build_assistant_context_pack()`, pass:

```python
available_tool_specs=tool_catalog.qualified_tool_specs,
active_skill_ids=set(tool_catalog.active_skill_ids),
```

Rename the capability argument to `qualified_tool_specs` and update its local names and prompt-safe reasons from unavailable to unqualified where applicable.

- [x] **Step 4: Update existing skill tests to use explicit activation**

For tests that expect `realtime_web_search`, construct the request with:

```python
metadata={"tool_visibility": {"enabled_skills": ["realtime_web_search"]}}
```

Tests for disabled, invalid, or under-permissioned skills must continue to expect no capability and an audit issue.

- [x] **Step 5: Run Task 2 tests and verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_phase3_skill_system_gate.py \
  tests/test_tool_catalog.py
```

Expected: all selected tests pass.

---

### Task 3: Runtime, Prompt, Validator, and Mock Compatibility

**Files:**
- Modify: `src/assistant_agent/agent/assistant_loop_nodes.py`
- Modify consumers of `RunToolSet.available_tool_names`
- Modify: `tests/test_native_tool_call_handoff.py`
- Modify: `tests/test_tool_call_boundaries.py`
- Modify: `tests/test_failure_recovery_policy.py`
- Modify: `tests/test_assistant_context_renderer.py`
- Modify: `tests/test_memory_media_ingestion.py`
- Modify: `tests/test_calendar_search_slice.py`
- Modify: `tests/test_prompt_compiler.py`

**Interfaces:**
- Consumes: Task 1 `RunToolSet` contract and Task 2 explicit skill filtering.
- Preserves: `ActionValidator -> ToolExecutor -> ToolRegistry`.

- [x] **Step 1: Write failing native integration expectation**

Replace the semantic-hidden native test with:

```python
def test_native_tool_call_may_choose_any_qualified_tool_without_keyword_routing() -> None:
    adapter = NativeToolChatAdapter(
        [
            native_result("web_search", {"query": "通勤耳机评测", "limit": 2}),
            final_result("已查询评测。"),
        ]
    )
    runtime = AgentGraphRuntime(config=ProviderConfig(), chat_adapter=adapter)

    state = runtime.run_state(
        UserRequest(user_id="u1", session_id="s1", text="帮我找通勤耳机")
    )

    first_turn_tools = [tool["function"]["name"] for tool in adapter.requests[0].tools]
    assert first_turn_tools == create_default_registry().list()
    assert [call.tool_name for call in state.tool_calls] == ["web_search"]
    assert state.response is not None
    assert state.response.message == "已查询评测。"
```

- [x] **Step 2: Run native integration test and verify RED**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_native_tool_call_handoff.py::test_native_tool_call_may_choose_any_qualified_tool_without_keyword_routing
```

Expected: FAIL because the current keyword selector hides `web_search` for the request.

- [x] **Step 3: Remove mock executable widening and migrate field consumers**

Delete `_authorize_mock_plan_tools()` and its call from `_build_decision_context()`. Replace every `available_tool_names` use with `qualified_tool_names`. Update manual `RunToolSet` fixtures so qualified names contain all exposed/executable names.

Update the mock recovery assertion to:

```python
assert state.run_tool_set is not None
assert state.run_tool_set.qualified_tool_names == state.run_tool_set.exposed_tool_names
assert state.run_tool_set.exposed_tool_names == state.run_tool_set.executable_tool_names
```

- [x] **Step 4: Replace semantic prompt-subset assertions**

Update renderer/context tests so identity recall expects every qualified spec:

```python
assert pack.prompt_tool_specs == tool_specs
assert pack.tool_catalog_summary.filtered_tool_count == 0
assert '"name": "render_3d"' in prompt
assert pack.tool_catalog_summary.selection_reasons == ["recall_identity"]
```

Update memory-media selection tests to assert both ordinary video and memory-ingestion requests expose qualified media tools; keep `ActionValidator` intent-gate tests unchanged. Update calendar tests to assert default-disabled or missing-env tools remain absent.

- [x] **Step 5: Run the affected runtime suite and verify GREEN**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_native_tool_call_handoff.py \
  tests/test_tool_call_boundaries.py \
  tests/test_failure_recovery_policy.py \
  tests/test_assistant_context_renderer.py \
  tests/test_memory_media_ingestion.py \
  tests/test_calendar_search_slice.py \
  tests/test_prompt_compiler.py
```

Expected: all selected tests pass.

---

### Task 4: Documentation and Verification

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/context_engineering_status.md`
- Keep: `docs/superpowers/specs/2026-07-13-tool-qualification-identity-recall-design.md`
- Keep: `docs/superpowers/plans/2026-07-13-tool-qualification-identity-recall.md`

**Interfaces:**
- Documents the final behavior delivered by Tasks 1-3.

- [x] **Step 1: Update authoritative documentation**

Document this exact chain:

```text
registry inventory
  -> structured qualification
  -> identity recall
  -> provider-native exposure
  -> LLM autonomous tool choice
  -> validator/executor risk enforcement
```

Remove claims that request keywords select prompt ToolSpec subsets, that low-confidence selection falls back to the visible full list, or that `available_tool_names` is a run-scoped set. State that recall is currently identity and future semantic recall requires high-recall recovery design.

- [x] **Step 2: Run environment and targeted verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_tool_catalog.py \
  tests/test_phase3_skill_system_gate.py \
  tests/test_native_tool_call_handoff.py \
  tests/test_tool_call_boundaries.py \
  tests/test_failure_recovery_policy.py \
  tests/test_assistant_context_renderer.py \
  tests/test_memory_media_ingestion.py \
  tests/test_calendar_search_slice.py \
  tests/test_prompt_compiler.py
```

Expected: environment check reports `ok: true`; targeted tests pass.

- [x] **Step 3: Run formatting and full-suite verification**

Run:

```bash
git diff --check -- AGENTS.md docs src tests .codex/skills
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: diff check passes. Report the exact full-suite counts and independently reproduce any failures before classifying them as unrelated.

- [x] **Step 4: Review the final diff without committing**

Run:

```bash
git status --short --untracked-files=all
git diff --stat
git diff --check
```

Confirm that no user-owned unrelated changes were reverted and no real provider, dependency, credential, or generated artifact was added.
