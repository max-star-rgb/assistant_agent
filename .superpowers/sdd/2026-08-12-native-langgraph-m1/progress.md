# SDD ledger — plan: docs/superpowers/plans/2026-08-12-native-langgraph-m1.md
Baseline: complete — commit 59cc6f58; POLICY-001/runtime/default pytest 15/15/86 passed.
Task 1: fix round 1/5 started — root checkpoint_ns is ignored; main spec governs: M1 disables root checkpointer, M2 owns real persistent namespace/resume.
Task 1: fix round 1/5 (3 addressed, 0 open; commits b838f1f3..0c22617e).
Task 1: complete — commits b838f1f3, 0c22617e; TDD 5/core 15/default 86 passed; M1 root checkpointer intentionally disabled per main spec.
Task 2: complete — native LangGraph v2 async stream normalized; root final values fail-closed; TDD 8/core runtime 15 passed.
Task 2: review APPROVED — 0 Critical/Important/Minor; reviewer reran TDD 8/core runtime 15 and real compiled graph probe.
Task 3: complete — native `arun_state` uses graph app `arun`; sync/async share prepare/finalize and run-context release; TDD 4/graph+runtime 27/default 86 passed; graph exceptions propagate without fake terminal events.
Task 3: review APPROVED — 0 Critical/Important/Minor; sync/async event and trace parity plus Deep Research branches verified.
Task 4: implementation complete — service stream awaits native async runtime; same-loop stream publication is direct; Gateway contracts unchanged; TDD 6/gateway 7/runtime+context 27/default 86 passed.
