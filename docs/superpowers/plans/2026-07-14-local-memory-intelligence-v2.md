# Local Memory Intelligence v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the built-in local memory core with typed facts, deterministic and auditable conflict resolution, confirmation for ambiguous replacements, and SQLite FTS5 hybrid retrieval without allowing any component to bypass `MemoryManager`, identity, policy, confirmation, audit, or context-budget boundaries.

**Architecture:** `MemoryManager` remains the only lifecycle boundary. Explicit writes are converted into an optional typed `MemoryFact`, evaluated by a pure `MemoryConflictResolver`, and then merged, superseded, coexisted, or routed into the existing durable confirmation workflow. SQLite adds a local FTS5 candidate index, while the existing retrieval strategy retains final identity/scope/status/expiry filtering, deterministic reranking, and bounded context construction.

**Tech Stack:** Python 3.11, Pydantic 2, SQLite/FTS5, existing `MemoryManager`/`MemoryStore` abstractions, pytest, offline memory evals. No new package or network dependency.

## Global Constraints

- The built-in local core is the primary implementation; external memory service behavior is out of scope.
- Keep `src/assistant_agent/tools/memory_tool.py` thin; conflict, merge, TTL, profile, and retrieval behavior belongs under `memory/` and `MemoryManager`.
- All durable writes must continue through `MemoryWritePolicy`, `MemoryManager`, identity binding, confirmation, and audit boundaries.
- LLM output may propose a typed fact or mutation, but it must never apply a mutation directly.
- Preserve legacy `preference_key`, `superseded_by_memory_id`, and `supersedes_memory_ids` fields for stored-data and API compatibility.
- Default tests and evals must remain mock/local/offline. Do not add a provider call.
- Do not install LangMem, Mem0, Graphiti, a vector database, an embedding model, or any other dependency in this plan.
- Do not modify external-memory adapters except where a shared protocol type must remain compatible.
- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest commands.
- Update `docs/memory-service-architecture.md` in the same implementation stage as behavior and tests; do not create a separate design-only commit.

## Non-goals and release gates

- Semantic contradiction inference is not a v2 guarantee. A conflict must have an explicit or deterministically derived `fact_key`.
- Automatic extraction from entire transcripts is not included. The assistant may pass structured content through the existing `memory_save` tool.
- Vector retrieval is deferred until the FTS5 eval gate demonstrates an unresolved recall problem.
- LangMem Core integration is a later opt-in phase requiring explicit dependency approval, a provider-free fake-adapter contract test, and an offline A/B eval against this baseline.
- Release requires backward-compatible loading of existing JSONL/SQLite `MemoryItem` payloads and schema-v3 SQLite databases.

## File structure

- Create `src/assistant_agent/schemas/memory_intelligence.py`: typed fact, conflict policy, provenance, status, and conflict-decision contracts.
- Create `src/assistant_agent/memory/facts.py`: pure parsing, normalization, legacy-field compatibility, and fact-state update helpers.
- Create `src/assistant_agent/memory/conflict_resolver.py`: pure deterministic conflict resolution over one candidate and identity-visible existing items.
- Modify `src/assistant_agent/memory/manager.py`: orchestrate conflict decisions, confirmation creation/application, audit, merge, profile rebuild, and legacy compatibility.
- Modify `src/assistant_agent/schemas/memory_audit.py`: distinguish policy confirmations from conflict confirmations and expose only prompt-safe conflict metadata.
- Modify `src/assistant_agent/memory/retrieval.py`: treat typed fact state as authoritative while retaining legacy supersede filtering.
- Modify `src/assistant_agent/memory/profile.py`: build profile entries only from active facts and keep normalized summary dedupe.
- Modify `src/assistant_agent/memory/store.py`: add an optional local candidate-search protocol without forcing remote stores to implement FTS.
- Modify `src/assistant_agent/memory/sqlite_store.py`: schema-v4 migration, FTS5 table/Python synchronization/backfill, candidate search, and index rebuild support.
- Modify `src/assistant_agent/memory/retriever.py`: use store-native candidates when available and deterministic keyword fallback otherwise.
- Modify `src/assistant_agent/memory/retrieval_eval.py` and `tests/evals/eval_cases.json`: add structured-conflict and lexical-retrieval gates.
- Modify `docs/memory-service-architecture.md`: make v2 local conflict and retrieval behavior authoritative.

---

### Task 1: Typed local fact contracts and compatibility helpers

**Files:**
- Create: `src/assistant_agent/schemas/memory_intelligence.py`
- Create: `src/assistant_agent/memory/facts.py`
- Test: `tests/test_memory_fact_contract.py`

**Interfaces:**
- Produces: `MemoryFact`, `MemoryFactStatus`, `MemoryFactProvenance`, `MemoryConflictPolicy`, `MemoryConflictAction`, and `MemoryConflictDecision`.
- Produces: `fact_from_item(item: MemoryItem) -> MemoryFact | None`, `fact_content(fact: MemoryFact) -> dict[str, Any]`, `normalize_fact_key(value: str) -> str`, and `mark_fact_superseded(item: MemoryItem, *, by_memory_id: str, at: datetime, reason: str) -> MemoryItem`.
- Consumes: existing `MemoryItem.content`, including legacy preference conflict fields.

- [ ] **Step 1: Write failing schema and compatibility tests**

```python
from datetime import datetime, timezone

import pytest

from assistant_agent.memory.facts import fact_from_item, normalize_fact_key
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.memory_intelligence import MemoryFact


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def test_memory_fact_rejects_invalid_validity_interval() -> None:
    with pytest.raises(ValueError, match="valid_to must be after valid_from"):
        MemoryFact(
            fact_key="user:preference:style",
            subject="user",
            predicate="preference.style",
            value="深色极简",
            provenance="user_explicit",
            observed_at=NOW,
            valid_from=NOW,
            valid_to=NOW,
        )


def test_fact_from_item_maps_legacy_preference_key() -> None:
    item = MemoryItem(
        memory_id="m1",
        user_id="u1",
        session_id="s1",
        memory_type="preference",
        summary="用户喜欢深色极简海报。",
        content={"preference_key": "style", "style": "深色极简"},
        source="explicit_user_request",
        created_at=NOW,
    )

    fact = fact_from_item(item)

    assert fact is not None
    assert fact.fact_key == "user:preference:style"
    assert fact.value == "深色极简"
    assert fact.status == "active"
    assert normalize_fact_key(" User / Preference / Style ") == "user:preference:style"
```

- [ ] **Step 2: Run the tests and verify the missing-module failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_fact_contract.py -q
```

Expected: FAIL during collection because `assistant_agent.schemas.memory_intelligence` does not exist.

- [ ] **Step 3: Implement the typed contracts**

Implement these exact public fields in `memory_intelligence.py`:

```python
MemoryFactStatus = Literal["active", "superseded", "disputed", "retracted"]
MemoryFactProvenance = Literal[
    "user_explicit", "user_confirmed", "tool_verified", "assistant_inferred", "imported"
]
MemoryConflictPolicy = Literal["replace", "coexist", "confirm"]
MemoryConflictAction = Literal["append", "merge", "supersede", "coexist", "confirm"]


class MemoryFact(BaseModel):
    schema_version: Literal[1] = 1
    fact_key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    value: str = Field(min_length=1)
    status: MemoryFactStatus = "active"
    provenance: MemoryFactProvenance
    conflict_policy: MemoryConflictPolicy = "confirm"
    observed_at: datetime
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    revision: int = Field(default=1, ge=1)
    supersedes_memory_ids: list[str] = Field(default_factory=list)
    superseded_by_memory_id: str | None = None
    conflict_reason: str | None = None


class MemoryConflictDecision(BaseModel):
    action: MemoryConflictAction
    reason: str
    fact_key: str | None = None
    matching_memory_ids: list[str] = Field(default_factory=list)
    superseded_memory_ids: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
```

Add a model validator that requires `valid_to > valid_from` when both exist, normalizes identifiers, removes duplicate supersede IDs, and rejects self-contradictory `active + superseded_by_memory_id` state.

- [ ] **Step 4: Implement pure compatibility helpers**

Store a typed fact under `content["fact"]`. `fact_from_item` must parse that form first, then map legacy preference fields using these deterministic rules:

```python
fact_key = f"user:preference:{normalized_preference_key}"
predicate = f"preference.{normalized_preference_key}"
value = str(content.get(normalized_preference_key) or item.summary).strip()
status = "superseded" if content.get("superseded_by_memory_id") else "active"
provenance = "user_explicit" if item.source == "explicit_user_request" else "imported"
conflict_policy = "replace"
```

Do not infer generic fact keys from arbitrary prose. Accept an explicit typed `content["fact"]` or `content["fact_key"]`; otherwise return `None` except for the legacy preference mapping.

- [ ] **Step 5: Run the focused tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_fact_contract.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the contracts**

```bash
git add src/assistant_agent/schemas/memory_intelligence.py src/assistant_agent/memory/facts.py tests/test_memory_fact_contract.py
git commit -m "feat(memory): add typed local fact contracts"
```

### Task 2: Pure deterministic conflict resolver

**Files:**
- Create: `src/assistant_agent/memory/conflict_resolver.py`
- Test: `tests/test_memory_conflict_resolver.py`

**Interfaces:**
- Consumes: `MemoryItem`, `MemoryFact`, and the existing effective governance scope rules.
- Produces: `MemoryConflictResolver.resolve(candidate: MemoryItem, existing: list[MemoryItem]) -> MemoryConflictDecision`.
- Does not write a store, emit audit events, or call an LLM.

- [ ] **Step 1: Write table-driven failing tests**

Cover these exact decisions:

```python
@pytest.mark.parametrize(
    ("policy", "candidate_value", "expected_action"),
    [
        ("replace", "深色极简", "supersede"),
        ("coexist", "深色极简", "coexist"),
        ("confirm", "深色极简", "confirm"),
    ],
)
def test_same_fact_key_different_value_uses_declared_policy(
    policy: str, candidate_value: str, expected_action: str
) -> None:
    existing = fact_item("old", value="浅色日系", policy=policy)
    candidate = fact_item("new", value=candidate_value, policy=policy)

    decision = MemoryConflictResolver().resolve(candidate, [existing])

    assert decision.action == expected_action
    assert decision.matching_memory_ids == ["old"]
    assert decision.requires_confirmation is (expected_action == "confirm")


def test_same_fact_value_merges_even_when_summary_wording_differs() -> None:
    decision = MemoryConflictResolver().resolve(
        fact_item("new", value="深色极简", summary="现在偏爱深色极简"),
        [fact_item("old", value="深色极简", summary="用户喜欢深色极简海报")],
    )
    assert decision.action == "merge"
    assert decision.matching_memory_ids == ["old"]


def test_different_governance_scope_never_conflicts() -> None:
    decision = MemoryConflictResolver().resolve(
        fact_item("new", value="深色", project_id="p2"),
        [fact_item("old", value="浅色", project_id="p1")],
    )
    assert decision.action == "append"
```

- [ ] **Step 2: Run the resolver tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_conflict_resolver.py -q
```

Expected: FAIL because `MemoryConflictResolver` does not exist.

- [ ] **Step 3: Implement the resolver rules**

The resolver must:

1. Ignore the candidate itself, `user_profile`, expired memories, non-active facts, and different tenant/project/effective scope.
2. Return `append` when the candidate has no typed fact or no active item shares its `fact_key`.
3. Return `merge` when at least one same-key active fact has the same normalized value.
4. Return `supersede`, `coexist`, or `confirm` for different values according to the candidate fact's `conflict_policy`.
5. Sort all returned memory IDs for deterministic audit and tests.
6. Use stable reasons: `no_structured_fact`, `no_active_fact_conflict`, `same_fact_value`, `replace_same_fact_key`, `coexist_same_fact_key`, and `confirmation_required_same_fact_key`.

Normalization is lowercase alphanumeric/CJK comparison only. It must not claim semantic equivalence.

- [ ] **Step 4: Run tests and static import validation**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_conflict_resolver.py tests/test_memory_fact_contract.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the resolver**

```bash
git add src/assistant_agent/memory/conflict_resolver.py tests/test_memory_conflict_resolver.py
git commit -m "feat(memory): resolve structured fact conflicts"
```

### Task 3: Apply conflict decisions through MemoryManager and durable confirmation

**Files:**
- Modify: `src/assistant_agent/schemas/memory_audit.py`
- Modify: `src/assistant_agent/memory/manager.py`
- Modify: `src/assistant_agent/tools/memory_tool.py`
- Test: `tests/test_memory_manager.py`
- Test: `tests/test_memory_lifecycle.py`
- Test: `tests/test_explicit_memory_e2e.py`

**Interfaces:**
- Consumes: `MemoryConflictResolver.resolve(candidate: MemoryItem, existing: list[MemoryItem])` and fact helpers from Tasks 1-2.
- Produces: all durable conflict changes through `MemoryManager._merge_or_save(item: MemoryItem) -> MemoryItem`.
- Extends: `MemoryPendingConfirmation.confirmation_kind: Literal["write_policy", "fact_conflict"]` and prompt-safe `conflict_memory_ids`/`fact_key`.

- [ ] **Step 1: Add failing manager tests for all four mutation paths**

Add tests that prove:

```python
def fact_payload(fact_key: str, value: str, policy: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "fact_key": fact_key,
        "subject": "user",
        "predicate": fact_key.removeprefix("user:").replace(":", "."),
        "value": value,
        "status": "active",
        "provenance": "user_explicit",
        "conflict_policy": policy,
        "observed_at": NOW.isoformat(),
        "confidence": 1.0,
        "revision": 1,
        "supersedes_memory_ids": [],
    }


def test_memory_manager_supersedes_generic_replace_fact() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    old = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我只能吃微辣",
        content={"fact": fact_payload("user:food:spice", "mild", "replace")},
        memory_id="spice_old",
        created_at=NOW,
    )
    new = manager.save_explicit(
        user_id="u1",
        session_id="s1",
        text="记住我现在喜欢重辣",
        content={"fact": fact_payload("user:food:spice", "hot", "replace")},
        memory_id="spice_new",
        created_at=NOW + timedelta(minutes=1),
    )
    assert store.get("u1", old.memory_id).content["fact"]["status"] == "superseded"
    assert store.get("u1", old.memory_id).content["fact"]["superseded_by_memory_id"] == new.memory_id
    assert new.content["fact"]["supersedes_memory_ids"] == [old.memory_id]


def test_memory_manager_merges_same_fact_value() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    first = manager.save_explicit(
        user_id="u1", session_id="s1", text="记住我喜欢重辣",
        content={"fact": fact_payload("user:food:spice", "hot", "replace")},
        memory_id="spice_first", created_at=NOW,
    )
    second = manager.save_explicit(
        user_id="u1", session_id="s2", text="记住我现在仍然喜欢重辣",
        content={"fact": fact_payload("user:food:spice", "hot", "replace")},
        memory_id="spice_second", created_at=NOW + timedelta(minutes=1),
    )
    assert second.memory_id == first.memory_id
    assert second.content["observation_count"] == 2


def test_memory_manager_keeps_coexisting_fact_values_active() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    first = manager.save_explicit(
        user_id="u1", session_id="s1", text="记住我常去上海",
        content={"fact": fact_payload("user:travel:city", "上海", "coexist")},
        memory_id="city_shanghai", created_at=NOW,
    )
    second = manager.save_explicit(
        user_id="u1", session_id="s1", text="记住我也常去杭州",
        content={"fact": fact_payload("user:travel:city", "杭州", "coexist")},
        memory_id="city_hangzhou", created_at=NOW + timedelta(minutes=1),
    )
    assert fact_from_item(first).status == "active"
    assert fact_from_item(second).status == "active"


def test_memory_manager_routes_ambiguous_fact_conflict_to_confirmation() -> None:
    store = InMemoryStore()
    manager = MemoryManager(store)
    manager.save_explicit(
        user_id="u1", session_id="s1", text="记住我在 A 公司工作",
        content={"fact": fact_payload("user:employment:company", "A", "confirm")},
        memory_id="company_a", created_at=NOW,
    )
    with pytest.raises(MemoryConfirmationRequired) as caught:
        manager.save_explicit(
            user_id="u1", session_id="s1", text="记住我在 B 公司工作",
            content={"fact": fact_payload("user:employment:company", "B", "confirm")},
            memory_id="company_b", created_at=NOW + timedelta(minutes=1),
        )
    assert caught.value.confirmation.confirmation_kind == "fact_conflict"
    assert caught.value.confirmation.fact_key == "user:employment:company"
    assert caught.value.confirmation.conflict_memory_ids
    assert len(store.list_by_user("u1")) == 2  # source item plus user_profile only
```

Also extend the e2e test to confirm the pending conflict and assert that confirmation re-runs conflict resolution with `user_confirmed` provenance before superseding the old fact.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_manager.py \
  tests/test_memory_lifecycle.py \
  tests/test_explicit_memory_e2e.py -q
```

Expected: new tests FAIL because generic facts and conflict confirmation are not applied.

- [ ] **Step 3: Extend the confirmation contract compatibly**

Add fields with backward-compatible defaults:

```python
MemoryConfirmationKind = Literal["write_policy", "fact_conflict"]

class MemoryPendingConfirmation(BaseModel):
    confirmation_kind: MemoryConfirmationKind = "write_policy"
    fact_key: str | None = None
    conflict_memory_ids: list[str] = Field(default_factory=list)
```

Never include conflicting memory summaries or raw values in confirmation list/audit metadata; IDs and normalized fact key are sufficient.

- [ ] **Step 4: Replace preference-only branching with resolver orchestration**

Refactor `_merge_or_save` into this sequence:

```python
decision = self.conflict_resolver.resolve(item, self.store.list_by_user(item.user_id))
if decision.action == "merge":
    return self._merge_items(existing_id=decision.matching_memory_ids[0], candidate=item)
if decision.action == "supersede":
    return self._save_with_supersedes(item, decision.superseded_memory_ids)
if decision.action == "confirm":
    raise self._conflict_confirmation_required(item, decision)
return self.store.save(item)
```

Keep `_find_duplicate` as the fallback for unstructured memories. Preserve legacy preference fields on both old and new records whenever the fact predicate begins with `preference.`.

- [ ] **Step 5: Make confirmation apply the selected action safely**

For `confirmation_kind="fact_conflict"`, rebuild the candidate from `redacted_payload`, set typed fact provenance to `user_confirmed`, set its conflict policy to `replace`, and invoke the normal `_merge_or_save` path. Do not directly update either memory from the API route or tool.

If any referenced conflict memory is no longer active when confirmation occurs, recompute against current store state; never trust the stale `conflict_memory_ids` list as authorization.

- [ ] **Step 6: Extend prompt-safe audit and tool output**

Audit metadata must include only:

```python
{
    "conflict_action": decision.action,
    "conflict_reason": decision.reason,
    "fact_key": decision.fact_key,
    "matching_memory_ids": decision.matching_memory_ids[:50],
    "superseded_memory_ids": decision.superseded_memory_ids[:50],
}
```

The tool result may continue returning the existing `confirmation_required` structure; add `confirmation_kind` and `fact_key`, but not memory values or summaries from older records.

- [ ] **Step 7: Run focused lifecycle tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_manager.py \
  tests/test_memory_lifecycle.py \
  tests/test_explicit_memory_e2e.py \
  tests/test_memory_tool_boundary.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit manager integration**

```bash
git add src/assistant_agent/schemas/memory_audit.py src/assistant_agent/memory/manager.py \
  src/assistant_agent/tools/memory_tool.py tests/test_memory_manager.py \
  tests/test_memory_lifecycle.py tests/test_explicit_memory_e2e.py
git commit -m "feat(memory): govern fact conflicts and confirmation"
```

### Task 4: Active-fact retrieval, profile repair, and conflict observability

**Files:**
- Modify: `src/assistant_agent/memory/retrieval.py`
- Modify: `src/assistant_agent/memory/profile.py`
- Modify: `src/assistant_agent/memory/manager.py`
- Modify: `src/assistant_agent/services/memory_audit.py`
- Test: `tests/test_memory_retrieval_strategy.py`
- Test: `tests/test_memory_lifecycle.py`
- Test: `tests/test_phase2_memory_intelligence_gate.py`

**Interfaces:**
- Consumes: `fact_from_item` and typed fact statuses.
- Produces: active-only default retrieval and profile projection; read-only debug still honors `include_superseded=True`.
- Preserves: legacy superseded exclusion and existing `MemoryProfileRepairResult` fields.

- [ ] **Step 1: Write failing retrieval and profile tests**

Add cases asserting:

- `superseded` and `retracted` typed facts are excluded by default.
- `include_superseded=True` includes `superseded` but not `retracted` items.
- `disputed` facts are excluded from automatic context and user profile, but visible in audit/list APIs.
- profile rebuild reports `profile_unresolved_conflicts` for two active same-key `confirm` facts imported from legacy data.
- project/tenant-scoped facts never enter the current global `user_profile`.
- legacy preference supersede tests remain unchanged and passing.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_retrieval_strategy.py \
  tests/test_memory_lifecycle.py \
  tests/test_phase2_memory_intelligence_gate.py -q
```

Expected: new typed-status cases FAIL.

- [ ] **Step 3: Centralize active-state checks**

Add and use these helpers from `facts.py`:

```python
def memory_fact_status(item: MemoryItem) -> MemoryFactStatus:
    fact = fact_from_item(item)
    return fact.status if fact is not None else (
        "superseded" if item.content.get("superseded_by_memory_id") else "active"
    )


def is_active_memory_fact(item: MemoryItem) -> bool:
    return memory_fact_status(item) == "active"
```

Remove duplicate local `_is_superseded` interpretations where possible. Keep the public query flag behavior backward compatible.

- [ ] **Step 4: Update profile rebuild and audit reporting**

Profile source selection must include only active, visible, unexpired, unscoped `preference`, `product`, and `task` items. Conflict groups must report stable fields:

```python
{
    "fact_key": fact_key,
    "active_memory_ids": ["active_memory_id"],
    "superseded_memory_ids": ["superseded_memory_id"],
    "disputed_memory_ids": ["disputed_memory_id"],
    "unresolved": True,
}
```

For legacy preference conflicts, retain `preference_key` in the same object so existing clients do not break.

- [ ] **Step 5: Run focused and API regression tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_retrieval_strategy.py \
  tests/test_memory_lifecycle.py \
  tests/test_memory_audit_api.py \
  tests/test_memory_snapshot_api.py \
  tests/test_phase2_memory_intelligence_gate.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit active-state behavior**

```bash
git add src/assistant_agent/memory/facts.py src/assistant_agent/memory/retrieval.py \
  src/assistant_agent/memory/profile.py src/assistant_agent/memory/manager.py \
  src/assistant_agent/services/memory_audit.py tests/test_memory_retrieval_strategy.py \
  tests/test_memory_lifecycle.py tests/test_phase2_memory_intelligence_gate.py
git commit -m "feat(memory): project only active facts"
```

### Task 5: SQLite schema-v4 FTS5 candidate index

**Files:**
- Modify: `src/assistant_agent/memory/store.py`
- Modify: `src/assistant_agent/memory/sqlite_store.py`
- Modify: `docs/development/memory-sqlite-operator-runbook.md`
- Test: `tests/test_memory_persistence.py`
- Test: `tests/test_memory_store_boundary.py`

**Interfaces:**
- Produces optional runtime-checkable `MemoryCandidateSearchStore.search_candidates(*, user_id: str, query: str, limit: int, memory_types: set[str] | None = None) -> list[MemoryItem]`.
- Implements the protocol only in `SQLiteMemoryStore`; other stores retain existing behavior.
- Migrates SQLite schema version from 3 to 4 without rewriting or losing `memory_items.payload`.

- [ ] **Step 1: Write failing migration and FTS tests**

Add tests that:

1. Open a schema-v3 fixture database, instantiate `SQLiteMemoryStore`, and assert schema version 4.
2. Assert every non-deleted v3 item is discoverable after migration/backfill.
3. Save/update/soft-delete/hard-delete an item and assert FTS candidate results track each operation.
4. Search Chinese text where the query phrase is represented by deterministic 2-4 character n-grams.
5. Run `rebuild_indexes()` and assert FTS results are unchanged.
6. Assert one user's query cannot return another user's memory.

Example contract assertion:

```python
items = store.search_candidates(
    user_id="u1",
    query="深色极简海报",
    limit=10,
    memory_types={"preference"},
)
assert [item.memory_id for item in items] == ["style_dark"]
assert items[0].relevance is not None
```

- [ ] **Step 2: Run persistence tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_persistence.py tests/test_memory_store_boundary.py -q
```

Expected: new tests FAIL because schema v4 and `search_candidates` do not exist.

- [ ] **Step 3: Add the optional candidate-search protocol**

Define without adding it to mandatory `MemoryStore`:

```python
@runtime_checkable
class MemoryCandidateSearchStore(Protocol):
    def search_candidates(
        self,
        *,
        user_id: str,
        query: str,
        limit: int,
        memory_types: set[str] | None = None,
    ) -> list[MemoryItem]:
        """Return user-isolated local text candidates ordered by text relevance."""
```

This avoids forcing JSONL, in-memory, hybrid-remote, or external lifecycle stores to implement SQLite behavior.

- [ ] **Step 4: Add schema-v4 FTS5 structures and migration**

Create `memory_items_fts` with FTS5 columns:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
    user_id UNINDEXED,
    memory_id UNINDEXED,
    memory_type UNINDEXED,
    summary,
    search_text,
    tokenize = 'unicode61'
)
```

Use explicit Python synchronization inside `save`, `delete`, `hard_delete`, `delete_by_session`, and `clear_user`, rather than triggers parsing JSON. Build `search_text` from normalized summary, tags, safe scalar content, fact key/predicate/value, and deterministic Chinese 2-4 character n-grams separated by spaces.

Migration must create the table, clear it, backfill all `deleted_at IS NULL` rows from their validated `MemoryItem` payload, then update `memory_schema_version` to 4 in the same transaction.

- [ ] **Step 5: Implement safe FTS querying**

Construct the MATCH expression only from locally tokenized alphanumeric/CJK tokens; quote each token and join with `OR`. Never interpolate raw query text into SQL. Bind `user_id`, optional memory types, and limit as parameters outside MATCH.

Convert SQLite `bm25` ordering into a bounded relevance score while preserving lower-rank-first ordering. Return full validated items by joining candidate `(user_id, memory_id)` pairs back to `memory_items.payload`.

- [ ] **Step 6: Update index rebuild and operator runbook**

`rebuild_indexes()` must rebuild B-tree indexes and fully backfill `memory_items_fts`. Document schema-v4 backup/restore compatibility, FTS availability check, rebuild command, and rollback limitation in `memory-sqlite-operator-runbook.md`.

- [ ] **Step 7: Run persistence and integrity tests**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_persistence.py tests/test_memory_store_boundary.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit SQLite FTS5**

```bash
git add src/assistant_agent/memory/store.py src/assistant_agent/memory/sqlite_store.py \
  docs/development/memory-sqlite-operator-runbook.md tests/test_memory_persistence.py \
  tests/test_memory_store_boundary.py
git commit -m "feat(memory): add SQLite FTS5 candidate search"
```

### Task 6: Hybrid local ranking and offline eval gates

**Files:**
- Modify: `src/assistant_agent/memory/retriever.py`
- Modify: `src/assistant_agent/memory/retrieval.py`
- Modify: `src/assistant_agent/memory/retrieval_eval.py`
- Modify: `tests/evals/eval_cases.json`
- Test: `tests/test_memory_retrieval_ranking.py`
- Test: `tests/test_memory_retrieval_eval.py`
- Test: `tests/test_memory_evals.py`

**Interfaces:**
- Consumes: optional `MemoryCandidateSearchStore` from Task 5.
- Produces: store-native FTS candidates for SQLite and unchanged deterministic keyword fallback elsewhere.
- Preserves: final filters and context budget in `MemoryRetrievalStrategy`.

- [ ] **Step 1: Write failing backend-parity and ranking tests**

Create identical fixture sets in `InMemoryStore` and `SQLiteMemoryStore` and assert:

- Both return the expected memory IDs for English identifiers and Chinese phrases.
- SQLite can recall a phrase that the whole-query keyword path misses.
- Exact structured fact-key/value match outranks loose summary overlap.
- Superseded/disputed/expired/sensitive/cross-user/cross-project items remain excluded as appropriate.
- Capability/type priority remains a deterministic tie-breaker after candidate relevance.
- `ranking_reason` becomes `local_text_match_type_priority_recency` for both local paths, avoiding backend-specific API promises.

- [ ] **Step 2: Run ranking tests and verify failure**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_retrieval_ranking.py tests/test_memory_retrieval_eval.py -q
```

Expected: new FTS/parity cases FAIL.

- [ ] **Step 3: Route candidate retrieval through the optional protocol**

Update `KeywordMemoryRetriever.search`:

```python
candidate_search = getattr(self.store, "search_candidates", None)
if callable(candidate_search):
    return candidate_search(
        user_id=user_id,
        query=query,
        limit=limit,
        memory_types=memory_types,
    )
return self._deterministic_scan(
    user_id=user_id,
    query=query,
    limit=limit,
    memory_types=memory_types,
)
```

Do not move identity, scope, superseded, disputed, expiry, sensitivity, or top-k enforcement into this adapter branch; those remain final service/retrieval/context checks.

- [ ] **Step 4: Add structured relevance signals**

In final ranking, add bounded deterministic boosts:

```python
fact_key_or_value_exact = 0.20
artifact_ref_present = 0.10
```

Clamp total relevance to `1.0`. Keep capability/type priority and recency as tie-breakers rather than allowing them to overwhelm a stronger text match.

- [ ] **Step 5: Extend offline eval fixtures and metrics**

Add cases for:

- generic food-spice replacement excludes the old fact;
- coexist city facts both remain retrievable;
- ambiguous employment conflict is not written before confirmation;
- Chinese paraphrase/fragment recall;
- current global fact does not leak into a different project when scoped;
- legacy preference supersede remains correct.

Extend eval results with `backend` and run the same local retrieval cases against `memory` and temporary `sqlite`; preserve the existing aggregate metric names and add per-backend breakdowns.

- [ ] **Step 6: Run the memory eval suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
```

Expected: exit 0; no cross-user leakage, sensitive/expired injection, superseded active recall, or token-budget violation; all newly added conflict cases pass for both local backends.

- [ ] **Step 7: Run focused pytest suites**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_retrieval_ranking.py \
  tests/test_memory_retrieval_eval.py \
  tests/test_memory_evals.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit hybrid retrieval and eval gates**

```bash
git add src/assistant_agent/memory/retriever.py src/assistant_agent/memory/retrieval.py \
  src/assistant_agent/memory/retrieval_eval.py tests/evals/eval_cases.json \
  tests/test_memory_retrieval_ranking.py tests/test_memory_retrieval_eval.py \
  tests/test_memory_evals.py
git commit -m "feat(memory): rank local FTS and keyword recall"
```

### Task 7: Authority documentation and final regression gate

**Files:**
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/context_engineering_status.md` only if the injected-memory status contract changed
- Test: existing memory, API, tool, runtime, and eval suites

**Interfaces:**
- Documents: typed fact schema, deterministic conflict policies, confirmation recomputation, active-state rules, SQLite FTS5, fallback behavior, and deferred framework integration.
- Produces no new runtime interface.

- [ ] **Step 1: Update the authority document**

Document these exact boundaries:

- `content["fact"]` is the typed v1 fact envelope; legacy preference fields remain readable.
- Same normalized fact value merges; `replace`, `coexist`, and `confirm` are explicit deterministic policies.
- Conflict confirmation stores prompt-safe metadata and recomputes against current state when accepted.
- `active` facts alone feed normal retrieval/context/profile; debug APIs may include superseded history; retracted facts never enter Agent context.
- SQLite FTS5 is candidate retrieval, not an authority or policy layer.
- In-memory and JSONL stores retain deterministic scan fallback.
- LangMem/Mem0/Graphiti are not runtime dependencies; any future adapter emits proposals and cannot write around `MemoryManager`.

- [ ] **Step 2: Run environment and targeted regression checks**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
```

Expected: all commands exit 0.

- [ ] **Step 3: Run the complete test suite**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest
```

Expected: PASS with no unexpected skips or network/provider calls.

- [ ] **Step 4: Validate formatting and scope**

Run:

```bash
git diff --check -- docs/memory-service-architecture.md docs/context_engineering_status.md \
  src/assistant_agent/memory src/assistant_agent/schemas src/assistant_agent/services \
  src/assistant_agent/tools/memory_tool.py tests
git status --short
```

Expected: `git diff --check` exits 0; status contains only files named by this plan and any pre-existing user changes.

- [ ] **Step 5: Commit code, tests, and authority docs together for the final stage**

```bash
git add docs/memory-service-architecture.md docs/context_engineering_status.md \
  src/assistant_agent/memory src/assistant_agent/schemas src/assistant_agent/services \
  src/assistant_agent/tools/memory_tool.py tests
git commit -m "docs(memory): define local intelligence v2 boundaries"
```

## Post-v2 decision gate: optional LangMem Core experiment

Do not execute this gate as part of the plan above. After v2 metrics are recorded, create a separate plan only if the user explicitly approves a new dependency. That plan must:

1. Define a storage-agnostic `MemoryMutationProposal` adapter contract.
2. Run LangMem Core with a scripted/fake chat adapter in default tests and no network.
3. Convert inserts/updates/deletes into proposals only; apply them through the existing validator, conflict resolver, confirmation, `MemoryManager`, and audit flow.
4. Compare write precision, conflict accuracy, Recall@k/MRR, false-positive rate, latency, and token cost against v2.
5. Keep the dependency optional and disabled in default `memory/jsonl/sqlite` profiles.
6. Reject adoption if it cannot improve the eval gate without weakening identity, safety, offline defaults, or deterministic replay.
