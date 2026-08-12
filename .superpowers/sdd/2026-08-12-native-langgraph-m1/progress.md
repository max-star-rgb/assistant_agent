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
Task 4: fix round 1/5 — production HTTP Gateway and Agent-Service composition roots now inject native async streams; pool leases through terminal/exception/cancel; commit 6f3dfd25.
Task 4: review APPROVED — prior Important ADDRESSED; 0 Critical/Important/Minor; 43 related tests and authority validator passed.
Task 4: fix round 1/5 started — production HTTP Gateway and Agent-Service composition roots still injected sync `run_request` and selected the `asyncio.to_thread` compatibility path.
Task 4: fix round 1/5 implementation complete — both production roots inject native async streams; pool lease spans terminal result/error/cancel; TDD 9 passed before full regression.
Task 5: implementation complete — native graph sync/async context inherits Experiment parent; real LLM/backend attempts create safe child runs; owned client lifecycle and observability failures are fail-open; TDD 12/related 69/default 86 passed.
Task 5: fix round 1/5 — removed all Tool output_ref values, safely projects tool-role JSON, and refuses competing root when parent lookup fails; commit 5d84db18; TDD 15/related 72/default 86 passed.
Task 5: fix round 2/5 — tool trace projections now use strict top-level/content type allowlists and data counts; commit c92f3708; TDD 15/related 72/default 86 passed.
Task 5: review APPROVED — all projection and parent-context findings ADDRESSED after round 2; no new Critical/Important; Task5 TDD 15 passed.
Task 5: fix round 1/5 — 2 Important addressed: Tool refs/tool-role JSON are metadata-safe; parent lookup failure cannot create a competing root; TDD 15/related 72/default 86 passed.
Task 5: fix round 1/5 implementation committed — 5d84db18; post-commit TDD 15/related 72/default 86/doc/compileall/diff all passed.
Task 5: fix round 2/5 — positive allowlists protect Tool message top-level/content; Tool output exposes data count only; TDD 15/related 72/default 86 passed.
Task 5: fix round 2/5 implementation committed — c92f3708; post-commit TDD 15/related 72/default 86/doc/compileall/diff all passed.
Task 6: implementation complete — Runtime Regression uses LangSmith aevaluate/current RunTree/native async graph; OTel binding helper removed; native tree audit is fail-closed; TDD 77/related legacy 31/default 86/doc/compileall/diff passed; no real Provider/network.
Task 6: verification note — concurrent pytest exposed one existing 50ms Task7 timing failure; isolated 1 passed and serial full related suite 77 passed; no Task7 code/test change.
