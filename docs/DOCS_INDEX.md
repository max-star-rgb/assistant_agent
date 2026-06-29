# Documentation Index

This index records the current documentation status after architecture and documentation cleanup. Historical `tasks/`, `prompts/`, and repository-local `skills/` construction materials were deleted after explicit user confirmation; remaining archive material is kept only when it still has reference value.

Status values:

- `canonical`: current authoritative entry or policy document
- `reference`: useful supporting reference, not the first entry point
- `historical`: completed planning/task history
- `archive-candidate`: should be moved out of the default reading path later
- `delete-candidate`: should be deleted only after human review
- `unknown`: cannot be judged from current context

| path | status | reason | action |
| --- | --- | --- | --- |
| `README.md` | canonical | Human-facing project entry; describes default mock/local/offline mode, main commands, and current documentation routing. | keep |
| `AGENTS.md` | canonical | Agent behavior constraints and repository workflow rules; points Codex to the guide, index, and task-specific canonical docs. | keep |
| `docs/CODEX_PROJECT_GUIDE.md` | canonical | Current authoritative Codex project guide created from code/config/docs audit. | keep |
| `docs/DOCS_INDEX.md` | canonical | Current authoritative documentation inventory and cleanup status. | keep |
| `docs/TESTS_REVIEW.md` | canonical | Current tests directory read-only review for future cleanup planning. | keep |
| `docs/context-engineering-walkthrough.md` | reference | Human-readable walkthrough for assistant context data flow, field lifecycle, memory boundary, compaction triggers, and debugging; use when explanation is needed, not as the default engineering entry. | keep |
| `docs/CONTEXT_ENGINEERING_STATUS.md` | canonical | Current assistant context, conversation compaction, context budget, tool observation compaction, session summary, context observability status, and new-dialogue quick handoff entry. | keep |
| `docs/architecture.md` | canonical | Current concise runtime architecture overview; matches assistant-loop and provider-native tool calling direction. | link-from-index |
| `docs/architecture-layers.md` | canonical | Current layer ownership and governance boundary document; required for architecture/rework decisions. | link-from-index |
| `docs/memory-service-architecture.md` | canonical | Current authoritative memory service architecture, boundary, routing, token-aware context injection, retrieval eval, SQLite-backed audit events/metrics, consent confirmation flow, profile repair, export/retention lifecycle, and update-rule entry. | keep |
| `docs/agent-communication-routing.md` | canonical | Current authoritative multi-agent instance routing, agent communication, gateway API, and A2A adapter boundary entry; tracks internal schemas/directory/routing policy/delegation policy/delegation context/local and outbound A2A transports/service, pilot readiness summaries/replay, local multi-runtime factory, opt-in local `delegate_to_agent`, `AgentGateway`, `/agents/run`, inbound agent card filtering, and `/a2a/rpc` JSON-RPC error taxonomy. | keep |
| `docs/capabilities.md` | canonical | Current capability list and default provider boundary. | link-from-index |
| `docs/configuration.md` | canonical | Current runtime profile and provider selector policy. | link-from-index |
| `docs/provider-setup.md` | canonical | Current real-provider opt-in setup and missing-config behavior. | link-from-index |
| `docs/security.md` | canonical | Current safety defaults, secret handling, and real-provider opt-in policy. | link-from-index |
| `docs/quickstart.md` | canonical | Current local mock/offline quickstart. | link-from-index |
| `docs/development.md` | canonical | Current development commands and coding constraints. | link-from-index |
| `docs/development/agent-control-plane-plan.md` | reference | Phased Local Multi-Agent Gateway + inbound/outbound A2A control-plane plan; use for gateway routing, delegation safety, delegation context/budget controls, A2A conformance, outbound pilot, and pilot-readiness work. | keep |
| `docs/development/agent-production-auth-observability-plan.md` | reference | Planned production-auth-ready and observability-ready Agent Control Plane hardening plan; use for AuthContext production boundary, control-plane observability, audit/replay hygiene, and pilot readiness hardening. | keep |
| `docs/development/memory-kernel-hardening-plan.md` | reference | Phased Memory Kernel hardening execution plan for future memory engineering; complements `docs/memory-service-architecture.md`. | keep |
| `docs/development/memory-sqlite-operator-runbook.md` | reference | Local SQLite memory backup, restore, integrity check, index rebuild, migration rollback, and corruption-response runbook for the Memory Kernel. | keep |
| `docs/development/context-engine-memory-policy-plan.md` | reference | Completed staged Context Engine + Memory Policy implementation log; use for historical decisions and phase traceability, not as the current context-engineering entry. | keep |
| `docs/demo-flows.md` | canonical | Current offline demo-flow guide. | link-from-index |
| `docs/deployment-local.md` | reference | Local deployment support documentation. | keep |
| `docs/observability-local.md` | reference | Local run/trace/tool-call observability reference. | keep |
| `docs/release-checklist.md` | reference | Release validation checklist; useful but not the first project guide. | keep |
| `docs/troubleshooting.md` | reference | Operational troubleshooting reference. | keep |
| `docs/real-provider-smoke-runbook.md` | reference | Manual real-provider smoke runbook; real provider use remains explicit opt-in only. | keep |
| `docs/real-provider-smoke-matrix.md` | reference | Real-provider smoke coverage matrix; not default execution guidance. | keep |
| `docs/phase8/README.md` | reference | Important Phase 8 architecture background, but some early statements must be checked against current code. | keep |
| `docs/phase8/assistant-loop-architecture-upgrade.md` | reference | Background design for assistant-loop upgrade; useful for ReAct reasoning and boundaries. | keep |
| `docs/phase8/planning-and-reflection-roadmap.md` | reference | Future/extended ReAct plan-mode and reflection background. | keep |
| `docs/phase8/memory-manager-boundary.md` | reference | Phase 8 MemoryManager boundary background; use `docs/memory-service-architecture.md` as the current canonical source. | keep |
| `docs/phase8/beta-trial.md` | reference | Beta trial/productization reference, not global architecture entry. | keep |
| `docs/phase8/phase8A_1_react_action_quality_hardening.md` | historical | Completed/current Phase 8A hardening task design and audit trail. | archive-later |
| `docs/phase8/phase8A_2_react_final_answer_handoff.md` | historical | Completed/current Phase 8A handoff task design and audit trail. | archive-later |
| `.local/docs-archive/2026-06-29/phase1-7-docs/**` | historical | Local-only ignored archive of old Phase 1-7 docs that were not tracked by git; kept out of the default docs tree. | keep-local |
| `haodanku-openapi-docs/AI使用说明.md` | reference | Required entry for Haodanku-related development. | keep |
| `haodanku-openapi-docs/接口目录.md` | reference | Required interface category map for Haodanku-related development. | keep |
| `haodanku-openapi-docs/平台接入规则与接口选择.md` | reference | Haodanku auth and interface selection reference. | keep |
| `haodanku-openapi-docs/错误码与状态码.md` | reference | Haodanku error handling reference. | keep |
| `haodanku-openapi-docs/interfaces/*.md` | reference | Category-specific Haodanku interface references. Read only when implementing related provider behavior. | keep |
| `demo_data/README.md` | reference | Safe local demo media/data policy. | keep |
| `.env.example` | reference | Placeholder-only local configuration template; not a real env file. | keep |
| `pyproject.toml` | reference | Package/test configuration rather than prose docs; needed for validation commands. | keep |
| `docs/archive/2026-06-26/hello_agent_latest.docx` | historical | Archived from `hello_agent_latest.docx`. Long 2026-06-10 design document. It has background value, but current architecture is better represented by code plus `docs/CODEX_PROJECT_GUIDE.md`. Binary format makes diff/review poor. | keep |
| `src/multimodal_agent/api/readme.md` | delete-candidate | Two-line API startup note duplicated by `README.md` and `docs/quickstart.md`; it lives under `src/**`, so do not touch it in this task. | delete-after-human-review |
| `.pytest_cache/README.md` | delete-candidate | Pytest-generated cache documentation, not repository docs. It is generated and should not be maintained as project documentation. | delete-after-human-review |
| `.local/memory/readme_memory.md` | unknown | Local memory note under `.local`; likely generated/local-only and not part of committed documentation. Needs human confirmation before any action. | keep |

## Cleanup Rules

- Historical `tasks/`, `prompts/`, and repository-local `skills/` materials are intentionally absent after explicit user confirmation. Do not recreate those top-level directories unless a new task explicitly needs them.
- Every `delete-candidate` requires human review before deletion.
- If a stale document still contains unique implementation history, archive it rather than delete it.
- Keep README, AGENTS, `docs/CODEX_PROJECT_GUIDE.md`, and `docs/DOCS_INDEX.md` synchronized.

## Cleanup Log

### 2026-06-26

Archived:

- `prompts/phase1-7/**` -> `docs/archive/2026-06-26/prompts/phase1-7/**`; later deleted on 2026-06-29 after user confirmation.
- `skills/phase8-runner/SKILL.md` -> `docs/archive/2026-06-26/skills/phase8-runner/SKILL.md`; later deleted on 2026-06-29 after user confirmation.
- `hello_agent_latest.docx` -> `docs/archive/2026-06-26/hello_agent_latest.docx`

Deleted:

- `FILE_TREE.txt`: stale template tree superseded by `docs/CODEX_PROJECT_GUIDE.md`.
- `prompts/phase8/.~lock.run-assistant-loop-mvp.md#`: editor lock metadata, not project documentation.

Retained:

- `src/multimodal_agent/api/readme.md`: still `delete-candidate`, but `src/**` is out of scope for this cleanup.
- `.pytest_cache/README.md`: still `delete-candidate`, but its reason does not explicitly say it was absorbed by `docs/CODEX_PROJECT_GUIDE.md` or README/AGENTS.

### 2026-06-29

Local-only cleanup:

- ignored, untracked `docs/phase1-7/**` was moved to `.local/docs-archive/2026-06-29/phase1-7-docs/**` to remove it from the default docs tree without creating hundreds of new tracked archive files.

Deleted after user confirmation:

- `tasks/phase1-7/**`
- `tasks/phase8/**`
- `prompts/phase8/**`
- `skills/**`
- `docs/archive/2026-06-29/tasks/**`
- `docs/archive/2026-06-29/prompts/**`
- `docs/archive/2026-06-29/skills/**`
- `docs/archive/2026-06-26/prompts/**`
- `docs/archive/2026-06-26/skills/**`
- `scripts/validate_skills.py`
- `tests/test_skills_validation.py`
