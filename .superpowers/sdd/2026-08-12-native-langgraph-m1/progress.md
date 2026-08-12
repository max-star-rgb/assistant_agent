# SDD ledger — plan: docs/superpowers/plans/2026-08-12-native-langgraph-m1.md

Baseline: complete — `59cc6f58`；POLICY-001/runtime/default pytest 15/15/86 passed。
Task 1: complete — `b838f1f3`, `0c22617e`；Runtime 单次编译 graph、native context schema、stable thread；主 spec 裁决 M1 root checkpointer disabled，持久 namespace/resume 归 M2；review approved。
Task 2: complete — `0d1db3fc`；LangGraph v2 stream 正规化，root final values 缺失 fail-closed；review approved。
Task 3: complete — `b26c1910`；`arun_state()` 原生异步 graph，sync/async 共用 prepare/finalize/cleanup；review approved。
Task 4: complete — `8243848d`, `6f3dfd25`；Service、HTTP Gateway 与 Agent-Service production roots 消费 native async stream，runtime lease 覆盖 terminal/error/cancel；review fix 1/5 后 approved。
Task 5: complete — `6128057a`, `5d84db18`, `c92f3708`；graph/LLM/governed Tool 原生 LangSmith child tree，projection 使用正向安全 schema，日常观测 fail-open；review fix 2/5 后 approved。
Task 6: complete — `d205ad8e`, `02039476`；Runtime Regression 使用 `Client.aevaluate()`/current RunTree/native graph，tree/type/example/feedback 完整性 fail-closed；LangSmith OTel binding 删除；review fix 1/5 后 approved。
Task 7: complete — `96971cc3`, `579b2cdb`；server canonical store 不再为 LangSmith 重建 OTel graph tree，无消费者专用 factory/store/config conversion 删除；review fix 1/5 后 approved。
Task 8: implementation complete — RUN-001/LOOP-001/IDENT-001 最小 core 经 mutation RED/GREEN；authority 对齐 M1 已实现事实与 M2/M5 边界；native TDD 50、LangSmith TDD 40、related core 51、default core 90 passed；validator/compileall/diff/deletion gates passed；未运行真实 LangSmith/Provider。
