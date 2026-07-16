# Phase 2 Memory Intelligence v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn existing memory primitives into a repeatable Memory Intelligence v1 gate for the Personal Realtime AI Assistant runtime.

**Architecture:** Phase 2 does not introduce a new Memory Brain, vector store, external memory service, or keyword override router. It hardens the current local-first path: LLM-proposed `assistant_candidate` memory is audit-only by default, explicit user memory can become durable profile memory, conflicting profile preferences are superseded deterministically, recall remains policy-gated and eval-measured, and sensitive explicit saves go through confirmation before durable write.

**Tech Stack:** Python, existing `MemoryManager`, `MemoryWritePolicy`, `MemoryReadPolicy`, `InMemoryStore`, memory tools, memory audit service/API contracts, pytest, existing memory eval runner.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Keep default paths mock/local/offline; do not call real providers.
- Do not add dependencies.
- Do not add embeddings, vector DB, external memory platform, or a new Memory Brain.
- Do not auto-save all user utterances.
- Do not store raw provider responses, secrets, base64/media payloads, or real user data.
- Keep memory tools thin; service behavior belongs in `MemoryManager`, `memory/`, or `services/memory_*`.
- Preserve LLM-first memory tool selection; do not replace `source_intent` with keyword/vector rules.

---

## Task 1: Add Phase 2 Memory Intelligence Gate

**Status:** Planned.

**Files:**
- Create: `tests/test_phase2_memory_intelligence_gate.py`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md`

**Interfaces:**
- Consumes: `MemoryManager`, `InMemoryStore`, `MemoryWritePolicy`, `RequestIdentity`, `MemoryQuery`, existing memory eval helpers.
- Produces: a single Phase 2 pytest gate that verifies candidate memory, profile memory, deterministic supersede, recall, confirmation, and eval metrics.

**Acceptance:**
- `assistant_candidate` memory records prompt-safe audit/candidate metadata and does not persist by default.
- Explicit user preference memory writes durable memory and updates `user_profile`.
- A newer explicit preference with the same `preference_key` supersedes the older active preference.
- Active recall excludes superseded preference memories and injects only the current profile source.
- Sensitive explicit memory creates a pending confirmation instead of a durable item; confirmation writes the item through the normal manager path.
- Local memory eval suite remains deterministic and reports no failed memory cases.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_retrieval_eval.py tests/test_memory_audit_api.py tests/test_native_tool_call_handoff.py -q
git diff --check -- docs/memory-service-architecture.md docs/roadmaps/personal-realtime-ai-assistant-roadmap.md tests/test_phase2_memory_intelligence_gate.py
```

## Task 2: Add Phase 2 Gate Command Set

**Status:** Planned.

**Files:**
- Modify: `docs/roadmaps/personal-realtime-ai-assistant-roadmap.md`

**Acceptance:**
- Roadmap Phase 2 Gate lists Memory Intelligence v1 invariants, not generic future Memory Brain work.
- Gate commands include the new Phase 2 gate, focused memory tests, memory eval suite, `pytest -m fast`, and `git diff --check`.

**Verification:**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_env.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py tests/test_memory_manager.py tests/test_memory_retrieval_eval.py tests/test_memory_audit_api.py tests/test_native_tool_call_handoff.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- AGENTS.md docs src tests scripts
```

## Scope Exclusions

- No embedding/vector retrieval.
- No external memory provider or remote memory platform.
- No new Memory Brain service.
- No episodic/semantic/procedural taxonomy redesign.
- No automatic raw transcript/user-utterance saving.
- No new frontend confirmation UX beyond existing service/API contracts.
- No skill system or multi-agent work in this phase.
