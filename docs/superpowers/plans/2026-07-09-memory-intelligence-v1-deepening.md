# Memory Intelligence v1 Deepening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete Phase2 Memory Intelligence v1 by adding a prompt-safe recall report and tightening the gate tests around confirmation rejection and the real native memory tool chain.

**Architecture:** Keep Memory Intelligence inside the existing Memory Service boundary. `MemoryManager` owns recall report construction and writes prompt-safe metadata onto `UserRequest`; memory tools remain thin adapters through `ActionValidator -> ToolExecutor -> MemoryManager`. Do not add a new Memory Brain, vector retrieval, external memory platform, LLM judge service, Gateway memory logic, or Skill behavior.

**Tech Stack:** Python 3, Pydantic models, existing `MemoryManager`, `MemoryReadPolicy`, `MemoryWritePolicy`, `InMemoryStore`, `AgentGraphRuntime` native tool-call tests, pytest, existing memory eval runner.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Do not add dependencies.
- Do not add embeddings, vector DB, external memory platform, or a new Memory Brain.
- Do not auto-save all user utterances.
- Do not store raw provider responses, secrets, base64/media payloads, or real user data.
- Keep memory tools thin; service behavior belongs in `MemoryManager`, `memory/`, or `services/memory_*`.
- Preserve LLM-first memory tool selection; do not replace `source_intent` with keyword/vector rules.
- Recall report must not include raw query text, raw memory content, raw prompt, raw user transcript, provider raw response, hidden reasoning, API keys, tokens, or media bodies.
- Tenant/project-scoped memories must not update global `user_profile`.
- Phase2 must not add realtime-memory scenarios until Phase1 loop gate stays green.

---

### Task 1: Prompt-Safe Recall Report

**Files:**
- Modify: `src/assistant_agent/memory/manager.py`
- Test: `tests/test_phase2_memory_intelligence_gate.py`
- Update: `docs/memory-service-architecture.md`

**Interfaces:**
- Consumes: `MemoryContext`, `MemorySearchResult`, `MemoryReadDecision`, `RequestIdentity`, `MemoryQuery`
- Produces: `MemoryContext.recall_report: dict[str, Any]` and `request.metadata["memory_recall_report"]`

- [x] **Step 1: Write failing recall report gate**

Add a test to `tests/test_phase2_memory_intelligence_gate.py` named `test_phase2_recall_report_is_prompt_safe_and_explains_active_recall`.

The test should:

```python
from assistant_agent.agent.state import AgentState
from assistant_agent.schemas.memory import MemoryItem
from assistant_agent.schemas.requests import UserRequest
```

Use `MemoryManager(InMemoryStore())`, create an old style preference, then a new style preference with the same `preference_key`, and add one sensitive style memory:

```python
old = manager.save_explicit(
    user_id="u1",
    session_id="s1",
    text="记住我喜欢浅色日系风格",
    content={"preference_key": "style", "style": "浅色日系", "summary": "用户喜欢浅色日系风格。"},
    memory_id="style_old",
    created_at=NOW,
)
new = manager.save_explicit(
    user_id="u1",
    session_id="s1",
    text="记住我现在喜欢深色极简风格",
    content={"preference_key": "style", "style": "深色极简", "summary": "用户喜欢深色极简风格。"},
    memory_id="style_new",
    created_at=NOW,
)
store.save(
    MemoryItem(
        memory_id="style_secret",
        user_id="u1",
        memory_type="preference",
        summary="用户有一个敏感风格偏好。",
        sensitivity="sensitive",
        created_at=NOW,
    )
)
```

Load memory with:

```python
request = UserRequest(user_id="u1", session_id="s2", text="按我保存的偏好继续风格方案")
state = AgentState.from_request(request)
context = manager.load_into_state(state, request, max_context_chars=1000)
report = state.request.metadata["memory_recall_report"]
serialized_report = str(report)
```

Assert:

```python
assert report["read_allowed"] is True
assert report["policy_reason"] == "explicit_memory_reference"
assert report["query_present"] is True
assert report["query_kind"] in {"saved_preference", "continuation", "history_reference"}
assert isinstance(report["query_hash"], str)
assert len(report["query_hash"]) == 64
assert "query" not in report
assert request.text not in serialized_report
assert "深色极简" not in serialized_report
assert "浅色日系" not in serialized_report
assert report["candidate_count"] >= len(context.items)
assert report["injected_count"] == len(context.items)
assert report["profile_source_ids"] == [new.memory_id]
assert report["superseded_excluded_count"] == 1
assert any("sensitive_memory_not_injected" in reason for reason in report["rejected_reasons"])
```

- [x] **Step 2: Run test to verify red**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py::test_phase2_recall_report_is_prompt_safe_and_explains_active_recall -q
```

Expected: fails because `memory_recall_report` is not present yet.

- [x] **Step 3: Implement minimal recall report**

In `src/assistant_agent/memory/manager.py`:

- add `recall_report: dict[str, Any] = Field(default_factory=dict)` to `MemoryContext`;
- set `state.request.metadata["memory_recall_report"] = context.recall_report` in `load_into_state`;
- add a private helper that builds a prompt-safe report without raw query or memory text;
- compute `query_hash` with `hashlib.sha256(normalized_query.encode("utf-8")).hexdigest()` when query is non-empty;
- compute `query_kind` from coarse markers only;
- compute `profile_source_ids` from identity-visible `user_profile.content["source_memory_ids"]`;
- compute `superseded_excluded_count` from identity-visible items with `content["superseded_by_memory_id"]`;
- include only counts, ids, policy reason, rejection reasons, retrieval version, and query metadata.

- [x] **Step 4: Run focused test to green**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py::test_phase2_recall_report_is_prompt_safe_and_explains_active_recall -q
```

Expected: passes.

---

### Task 2: Confirmation Rejection And Tool-Chain Gates

**Files:**
- Modify: `tests/test_phase2_memory_intelligence_gate.py`
- No production changes expected unless the tests expose a real gap.

**Interfaces:**
- Consumes: `MemoryConfirmationRequired`, `MemoryManager.reject_memory_for_identity(...)`, `AgentGraphRuntime` native memory tool path.
- Produces: Phase2 gate coverage for rejection and native memory save chain.

- [x] **Step 1: Add rejection gate test**

Add `test_phase2_sensitive_explicit_memory_rejection_never_writes_durable_memory` to `tests/test_phase2_memory_intelligence_gate.py`:

```python
with pytest.raises(MemoryConfirmationRequired) as raised:
    manager.save_explicit_for_identity(identity, text="记住我的项目路径是 /home/alice/private/project")

rejected = manager.reject_memory_for_identity(identity, raised.value.confirmation.confirmation_id)

assert rejected is not None
assert rejected.status == "rejected"
assert manager.list_for_identity(identity) == []
events = manager.list_audit_events_for_identity(identity)
assert events[-1].event_type == "memory_confirmation_decided"
assert events[-1].outcome == "rejected"
assert events[-1].metadata["status"] == "rejected"
```

- [x] **Step 2: Add native tool-chain gate command to the plan and docs**

Do not duplicate the existing integration test. Keep `tests/test_native_tool_call_handoff.py::test_native_memory_save_only_when_llm_selects_tool` in Phase2 verification so the real path remains covered:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py::test_native_memory_save_only_when_llm_selects_tool -q
```

- [x] **Step 3: Run focused tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py::test_native_memory_save_only_when_llm_selects_tool -q
```

Expected: both pass.

---

### Task 3: Documentation Sync

**Files:**
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/superpowers/specs/2026-07-09-memory-intelligence-v1-design.md`
- Modify: this plan file

**Interfaces:**
- Consumes: implemented `memory_recall_report` metadata.
- Produces: authority docs describing recall report and Phase2 gate commands.

- [x] **Step 1: Update memory architecture**

Add `request.metadata["memory_recall_report"]` to the list of memory metadata outputs and document:

- report excludes raw query text;
- includes `query_present`, `query_kind`, and `query_hash`;
- includes counts, injected ids, profile source ids, superseded exclusion count, omitted count, and rejected reasons;
- is developer/debug metadata, not a learning loop.

- [x] **Step 2: Update spec if implementation changes names**

If the implemented field names differ from the spec, update `docs/superpowers/specs/2026-07-09-memory-intelligence-v1-design.md` so the design remains accurate.

- [x] **Step 3: Run doc checks**

```bash
rg -n "query: str|raw query|Judge|TBD|TODO|todo|待定|implement later|fill in" docs/superpowers/specs/2026-07-09-memory-intelligence-v1-design.md docs/memory-service-architecture.md
git diff --check -- docs/memory-service-architecture.md docs/superpowers/specs/2026-07-09-memory-intelligence-v1-design.md docs/superpowers/plans/2026-07-09-memory-intelligence-v1-deepening.md
```

Expected: no unsafe raw-query requirement and no placeholders.

---

### Task 4: Verification And Phase Commit

**Files:**
- All modified implementation, tests, docs, and plan files.

- [x] **Step 1: Run memory gates**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_write_policy.py tests/test_memory_read_policy.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_retrieval_eval.py tests/test_memory_audit_api.py tests/test_memory_tool_boundary.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_native_tool_call_handoff.py::test_native_memory_save_only_when_llm_selects_tool -q
```

- [x] **Step 2: Run memory eval**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
```

- [x] **Step 3: Run Phase1 loop gate because Phase2 depends on it**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase1_realtime_loop_deep_gate.py tests/test_realtime_call_simulator.py -q
```

- [x] **Step 4: Run fast suite and diff check**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- AGENTS.md docs src tests scripts
```

- [x] **Step 5: Commit Phase2**

Stage only Phase2-related files, not unrelated `.gitignore` changes, and commit:

```bash
git add docs/memory-service-architecture.md docs/superpowers/specs/2026-07-09-memory-intelligence-v1-design.md docs/superpowers/plans/2026-07-09-memory-intelligence-v1-deepening.md src/assistant_agent/memory/manager.py tests/test_phase2_memory_intelligence_gate.py
git commit -m "参考hermes的长期个人助手:phase2-memory-intelligence"
```
