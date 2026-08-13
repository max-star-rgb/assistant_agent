# SDD ledger — plan: docs/superpowers/plans/2026-08-12-native-langgraph-m3.md

Gate 0: persistent_saver_gate=pending — M3 branch has no official async SQLite saver dependency, `open_async_checkpointer()`, cross-Runtime recovery test, or process-owned async saver lifecycle evidence. Task 1–4 may use `InMemorySaver` only; no dependency or lock modification is authorized.

Task 1: DONE — commits `6a62830d` + `4b07ed81`; strict Workflow graph state, ACI result ledger reducer, execution discriminator, branch-local Runtime Context, persisted budget narrowing, explicit legacy claim allowlists, and fresh-runtime recovery equivalence. Independent review fix round 1: APPROVED. Persistent saver gate remains pending; no cross-process durability claim.

Task 2: fix round 1/5 — constraints 12/64 mismatch addressed by `a670747d`; 0 open findings.
Task 2: DONE — commits `ea704318` + `a670747d`; native visible `AssistantTurnGraph.planner`, deterministic Workflow v2 admission, typed planning state/identity, fresh branch contexts, review clean. Persistent saver gate remains pending; InMemorySaver evidence only.

Task 3: DONE (offline) — commits `1838a6cd` + `9232dd8b` + `af7d5eea`; native conditional `Send`, deterministic arbitrary-DAG waves, Pregel join and isolated worker profile branches. No production cutover claim.

Task 4: DONE (offline) — commits `a355f982` + `4846907b` + `88406dbf`; verifier profile, `Command` repair/publish/fail routing, native retry/timeout/error handler and minimal generation repair.

Task 5: PARTIAL — commits `2674efaa` + `9592a74b` + `a3bbdbef`; parent-owned single/multi interrupt, same-thread/new-run resume, state history and publish barrier pass with `InMemorySaver`. Official persistent SQLite saver and cross-process recovery remain pending, so Task 5 is not complete.

Task 6: PREWORK — commits `0f736692` + `9c994224` + `718d77fc` + `1497c57c`; strict product snapshot/event projection, media consumer contract and terminal consistency are ready. `WorkflowGraphHost`, API/runtime composition cutover and production Deep Research migration are not implemented; legacy worker remains required.

Task 7: OFFLINE PREWORK — commits `8a829875` + `c0809731` + `f1a65960` + `8e69f728`; typed workflow Dataset/evaluator/trace-tree contracts and offline inspect are ready. Real LangSmith Dataset/Experiment/tree/Feedback operator evidence remains pending.

Task 8: OFFLINE PREWORK — native graph scheduler-negative runtime spy, source gates, authority reconciliation and full offline verification only. No legacy Deep Research deletion is authorized before Task 5/6 gates close; M3 acceptance remains pending.
