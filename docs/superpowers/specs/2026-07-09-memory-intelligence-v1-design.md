# Memory Intelligence v1 Design

## Goal

把 Phase 2 从“已有 Memory Service”做深到第一版可开发的 Personal Assistant Memory Intelligence：

```text
Realtime / Chat Conversation
  -> memory_save(source_intent=assistant_candidate | user_explicit)
  -> Memory Candidate Gate
  -> WriteDecisionPolicy / MemoryWritePolicy
  -> Pending Confirmation | Durable Memory
  -> User Profile Supersede
  -> Policy-Gated Recall
  -> Context Injection
  -> Memory Eval / Audit
```

这不是一个新的 Memory Brain，也不是向量数据库工程。目标是让个人助理开始稳定做到四件事：

- 知道什么只是“可能值得记”的候选。
- 知道什么是用户明确要求记住的事实或偏好。
- 能把稳定偏好维护成当前 profile。
- 能在用户明确需要历史上下文时召回正确记忆。

## Current State

当前项目已经具备 Phase 2 的大部分底座：

- `MemoryManager` 是记忆读写、profile upsert、confirmation、audit、retrieval 的服务边界。
- `MemoryWritePolicy` 已区分 explicit save、promotion candidate、自动写入禁用、敏感内容确认。
- `MemoryReadPolicy` 已限制长期记忆读取：普通建议、生成、搜索、首次请求不自动查长期记忆。
- `memory_save` / `memory_retrieval` 是薄工具适配器，工具内不拥有检索排序、写入策略或 profile merge。
- `source_intent` 已存在：
  - `user_explicit`
  - `assistant_candidate`
  - `user_confirmed`
- `assistant_candidate` 已默认 audit-only，不直接写长期记忆。
- 显式 preference memory 已能更新 `user_profile`。
- 同一 `preference_key` 的新偏好已能 supersede 旧偏好。
- 敏感显式记忆已能进入 pending confirmation。
- `scripts/run_evals.py --suite memory` 已有 deterministic memory eval。
- `tests/test_phase2_memory_intelligence_gate.py` 已覆盖候选、profile、supersede、confirmation、eval 的第一层 gate。

所以 Phase 2 深化不是“从零实现记忆系统”，而是把现有 primitives 变成个人助理可持续演进的窄闭环。

## Non-Goals

本阶段不做：

- embedding、vector DB、语义向量召回。
- 外部 Memory Server 作为默认记忆平台。
- 新的 Memory Brain 服务。
- 人类记忆模拟分类体系重构，例如先设计完整 episodic / semantic / procedural taxonomy。
- 自动保存所有用户原话。
- 自动从每轮通话写长期记忆。
- 从 trace 或 trajectory 自动改写 profile。
- RL、自动学习 pipeline、prompt 自动优化。
- Skill memory schema。
- 多 agent 共享记忆或 child agent 继承 parent memory。
- 新前端记忆管理中心。

Phase 2 只做个人助理最小可用的记忆智能闭环。

## Design Options

### Option A: Keep Existing MemoryManager And Add Tests Only

只补 gate test 和 eval，把现有 `MemoryManager` 行为固定下来。

优点是改动最少，风险低。缺点是“Memory Intelligence”只停留在测试描述里，缺少一个清楚的数据流和 debug 面，后续 Phase3 Skill 使用记忆时容易再次分叉。

### Option B: Add A Narrow Memory Intelligence Layer Inside Memory Service

在现有 Memory Service 边界内建立更清晰的 v1 概念：

```text
Candidate
  -> WriteDecisionPolicy
  -> Decision
  -> Profile Update / Confirmation / Audit
  -> Recall Report
```

这可以先是 `MemoryManager` 内部方法、数据模型和测试 gate，不需要新服务进程，也不改变工具边界。

优点是能把用户显式记忆、助手候选、profile 更新、召回报告串起来。缺点是要小心不要把它做成第二套 Memory Runtime。

### Option C: Build A New Memory Brain

新增一个独立 Memory Brain，负责候选抽取、冲突判断、长期 profile、召回、学习。

优点是概念完整。缺点是过早架构化，会绕开当前已经成熟的 `MemoryManager`、`MemoryWritePolicy`、`MemoryReadPolicy` 和工具治理边界。

### Recommended Approach

采用 Option B，但实现方式必须克制：

- 不新增独立 Memory Brain。
- 不引入新存储后端。
- 不改变 memory tools 的薄适配定位。
- 先把“候选、判断、profile、召回报告、eval gate”沉淀成 `MemoryManager` 及 `memory/` 层的可测试能力。
- 如果需要新 helper，只能放在 `src/assistant_agent/memory/` 或 `src/assistant_agent/services/memory_*`，不能放进 `tools/memory_tool.py` 或 context renderer。

## Core Vocabulary

### Conversation

当前会话输入、session history、context summary 和 realtime turn 都属于 conversation/context 层。

它们不是长期记忆。只有通过 policy 和 manager 之后，才可能产生 durable memory。

### Memory Candidate

Memory candidate 是“可能值得记”的建议，不是长期记忆。

来源可以是：

- assistant loop 调用 `memory_save(source_intent=assistant_candidate)`。
- run-summary path 构造 `MemoryPromotionCandidate`。
- 后续 Phase1 realtime trace 中提取出的安全候选。

v1 默认行为：

- candidate 只进入 audit / debug metadata。
- 不写 store。
- 不进入 `user_profile`。
- 不注入 context。

### WriteDecisionPolicy

WriteDecisionPolicy 是判断候选或显式写入能否进入 durable memory 的规则集合。v1 不新增独立判断类，不新增 LLM 判断服务，也不增加独立 Memory Brain。

v1 判断由现有组件承担：

- `MemoryWritePolicy.evaluate_explicit_save(...)`
- `MemoryWritePolicy.evaluate_promotion_candidate(...)`
- `MemoryItem` payload validation
- confirmation service
- deterministic profile supersede rules

未来如果引入 LLM 辅助判断，也必须只输出 `source_intent`、`source_reason`、`future_use`、`evidence` 或 candidate metadata，不能绕过 policy 直接写 store。

### Durable Memory

Durable memory 是已经通过 `MemoryManager -> MemoryWritePolicy -> MemoryItem -> MemoryStore` 的长期记忆。

v1 允许的主路径：

- 用户明确要求记住：`source_intent=user_explicit`
- 用户确认 pending memory：confirmation service 内部使用 `user_confirmed`

v1 默认不允许：

- `assistant_candidate` 自动写 durable memory。
- run summary 自动写 durable memory。
- raw transcript 自动写 durable memory。

### User Profile

`user_profile` 是一个普通 memory item：

```text
memory_id = user_profile
memory_type = preference
source = user_profile
```

它不是单独的数据库，也不是不可审计的隐藏状态。它由显式 preference / product / task memory 派生。

v1 profile 只解决：

- 当前稳定偏好。
- source memory ids。
- 同 key 偏好的 deterministic supersede。
- profile status / rebuild。

Tenant/project-scoped memories 不更新全局 `user_profile`。它们可以被 identity-scoped retrieval 读取，但 scoped profile 需要单独 schema/index 设计，不进入 Phase2。

### Recall

Recall 是被 policy 允许的长期记忆读取。

v1 召回原则：

- 用户明确表达历史上下文需求，才自动或工具读取。
- 当前用户输入和新工具结果优先于旧记忆。
- superseded memory 默认不参与 active recall。
- sensitive / expired memory 不注入 context。
- recall 结果必须带 trust policy / usage hint。

## Target Architecture

```text
Entry / Realtime / Chat
        |
        v
AgentGraphRuntime
        |
        +--> MemoryReadPolicy
        |       |
        |       v
        |   MemoryManager.load_context_for_request
        |       |
        |       v
        |   AssistantContextPack
        |
        +--> LLM decides tool call
                |
                v
        ActionValidator
                |
                v
        ToolExecutor
                |
                v
        memory_save / memory_retrieval
                |
                v
        MemoryManager
          |        |          |
          |        |          +--> MemoryAudit / Metrics / Eval
          |        |
          |        +--> MemoryWritePolicy / Confirmation
          |
          +--> MemoryStore / User Profile / Recall
```

Critical boundary:

```text
Tool
  -> adapts input/output only

MemoryManager
  -> owns write/read/profile/confirmation/audit behavior

Context
  -> consumes memory context only

Agent Runtime
  -> decides when to call tools
```

No Phase2 implementation should move these responsibilities across boundaries.

## Memory Write Flows

### Assistant Candidate

```text
LLM sees possible stable preference
  -> calls memory_save(
       source_intent="assistant_candidate",
       source_reason=...,
       future_use=...,
       evidence=...
     )
  -> ActionValidator allows only if schema is complete
  -> ToolExecutor runs memory_save
  -> MemoryManager records candidate/audit
  -> no durable write
```

Acceptance:

- Store remains unchanged.
- Audit event records candidate decision.
- Candidate metadata is prompt-safe.
- Trace/API summaries do not contain raw user text or raw memory content.

### User Explicit Memory

```text
User: 以后记住我喜欢短句回答
  -> LLM calls memory_save(source_intent="user_explicit")
  -> ActionValidator checks source_intent
  -> ToolExecutor runs memory_save
  -> MemoryManager.save_explicit_for_identity
  -> MemoryWritePolicy.evaluate_explicit_save
  -> MemoryItem validation
  -> MemoryStore.save
  -> profile upsert if applicable
```

Acceptance:

- Durable item is written only for runtime-bound identity.
- Model-supplied `user_id` cannot override `ToolContext.user_id`.
- Profile updates only through `MemoryManager`.
- Tool result returns structured data, not loose text.
- The real tool chain is covered: LLM/native tool call output -> `ActionValidator` -> `ToolExecutor` -> `memory_save` -> `MemoryManager`.

### Sensitive Explicit Memory

```text
User asks to remember sensitive-looking content
  -> MemoryWritePolicy requires confirmation
  -> MemoryManager creates MemoryPendingConfirmation
  -> no durable item yet
  -> user confirms through confirmation service/API
  -> manager re-runs safe builder
  -> durable memory written with confirmation metadata
```

Acceptance:

- Pending confirmation preview is redacted.
- Store has no durable item before confirmation.
- Confirmation writes through normal manager path.
- Rejection writes no durable item.

### Profile Supersede

```text
old explicit preference:
  preference_key = style
  summary = 用户喜欢浅色日系风格

new explicit preference:
  preference_key = style
  summary = 用户喜欢深色极简风格

MemoryManager:
  old.content.superseded_by_memory_id = new.memory_id
  new.content.supersedes_memory_ids = [old.memory_id]
  user_profile.source_memory_ids = [new.memory_id]
```

Acceptance:

- Deterministic key-based conflict handling.
- Active recall excludes superseded old preference.
- Debug/snapshot can include superseded chain only when explicitly requested.
- No LLM semantic conflict merge in v1.

## Memory Read Flow

### Automatic Load

```text
UserRequest
  -> load_memory node
  -> MemoryReadPolicy.decide
  -> if skipped:
       metadata.memory_context_skipped = true
       no store search
  -> if allowed:
       MemoryManager.search_for_identity
       MemoryContextBuilder
       request.metadata.memory_context_*
       AssistantContextPack
```

Acceptance:

- Ordinary first-turn generation/advice/search does not read long-term memory.
- Explicit continuation/history requests can read memory.
- Skipped reads are observable through prompt-safe metadata.

### Tool Retrieval

```text
LLM calls memory_retrieval
  -> ActionValidator read-intent gate
  -> ToolExecutor
  -> memory_retrieval tool
  -> MemoryManager.search_for_identity
  -> ToolResult with trust_policy and usage_hint
```

Acceptance:

- Non-empty query alone is not enough.
- Retrieval cannot cross user identity.
- Superseded, expired, or sensitive memory is not injected into active context.
- Memory is evidence, not instruction.

## Recall Report

Phase2 should expose a small developer-facing recall report in tests and trace metadata. It does not need a new API at first.

Minimum fields:

```text
read_allowed: bool
policy_reason: str
query_present: bool
query_kind: "empty" | "keyword" | "continuation" | "saved_preference" | "history_reference"
query_hash: str | null
candidate_count: int
injected_count: int
omitted_count: int
rejected_reasons: list[str]
retrieval_version: str
profile_source_ids: list[str]
superseded_excluded_count: int
```

Rules:

- Do not include raw query text. A query hash or coarse kind is enough for debugging.
- No raw memory content.
- No raw prompt.
- No raw user transcript.
- Stable enough for regression tests.

This report is the bridge between Phase2 and future debugging/learning work. It is not a learning loop.

## Realtime Dependency

Phase1 made text realtime turns stable:

```text
run.started
stream.chunk*
run.end(reason=completed | cancelled)
interrupt/cancel/hangup gates
```

Phase2 should use that stability in a narrow way:

- Completed turns can produce assistant candidates only when the LLM explicitly calls `memory_save`.
- Cancelled/interrupted turns should not auto-promote memory.
- Tool-running interrupt should not create memory from stale output.
- Phase2 implementation must wait for the Phase1 loop gate to stay green before adding any realtime-memory scenario.
- Realtime simulator can later add a memory scenario, but only after memory gates pass in direct tests.

Do not make Gateway own memory decisions. Gateway may carry session/run identity and trace metadata; memory remains inside Agent Runtime / MemoryManager / ToolExecutor boundaries.

## Observability And Audit

Every Phase2 path must be answerable without raw data:

- Did the run try to save memory?
- Was it `assistant_candidate`, `user_explicit`, or rejected?
- Was a confirmation required?
- Was a durable memory written?
- Did profile update?
- Did a supersede happen?
- Was recall skipped or allowed?
- Which memory ids were injected?
- Were any memories omitted because of sensitivity, expiration, budget, or supersede?

Prompt-safe event names can remain the existing memory audit events. If new names are needed, keep them memory-owned, for example:

```text
memory.candidate.recorded
memory.write.confirmation_required
memory.profile.superseded
memory.recall.reported
```

Do not put raw memory content, raw user text, provider raw response, base64/media body, API keys, tokens, or hidden reasoning into trace or audit output.

## Eval Requirements

Phase2 is not complete without deterministic eval.

Required local checks:

- Candidate audit-only behavior.
- Explicit profile write.
- Profile supersede.
- Active recall excludes superseded source.
- Sensitive explicit write requires confirmation.
- Confirmation rejection leaves no durable memory item and is covered by gate tests.
- Cross-user leakage remains zero.
- Sensitive injection remains zero.
- Expired injection remains zero.
- Correct-empty recall remains stable.
- Token budget compliance remains stable.

Embedding/vector work is blocked unless local eval proves keyword/phrase recall is insufficient on real cases.

## Acceptance Scenarios

| scenario | expected result |
| --- | --- |
| assistant candidate | candidate is audited, store unchanged |
| explicit preference | durable preference item written and profile updated |
| preference update | new preference supersedes old one deterministically |
| active recall | recall returns current preference, not superseded one |
| sensitive memory | pending confirmation created; no durable item before confirm |
| confirm memory | confirmation writes redacted durable item through manager |
| reject memory | confirmation rejected; no durable item |
| native memory tool chain | native tool call with `source_intent=user_explicit` passes validator, executes through ToolExecutor, and writes via MemoryManager |
| ordinary first request | memory context skipped; no store search |
| historical follow-up | memory read allowed and reported |
| cancelled realtime turn | no automatic memory promotion from cancelled output |
| tool-running interrupt | stale tool output cannot become candidate or durable memory |
| eval suite | memory eval remains green without vector dependencies |

## Implementation Boundaries

Phase2 implementation may touch:

- `src/assistant_agent/memory/manager.py`
- `src/assistant_agent/memory/write_policy.py`
- `src/assistant_agent/memory/read_policy.py`
- `src/assistant_agent/memory/retrieval.py`
- `src/assistant_agent/memory/context_builder.py`
- `src/assistant_agent/services/memory_audit.py`
- `src/assistant_agent/services/memory_observability.py`
- `src/assistant_agent/tools/memory_tool.py` only for thin input/output adaptation
- `tests/test_phase2_memory_intelligence_gate.py`
- focused memory tests and memory eval cases
- `docs/memory-service-architecture.md` only when behavior actually changes

Phase2 implementation must not touch:

- Gateway turn control logic.
- Realtime ASR/TTS/media handling.
- Skill loader or skill execution.
- Agent routing / multi-agent delegation.
- Provider selection to enable real external services.
- `tools/memory_tool.py` for policy, retrieval ranking, profile merge, TTL, audit, or direct store access.

## Testing Strategy

Use TDD for implementation.

Focused tests:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_phase2_memory_intelligence_gate.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_manager.py tests/test_memory_write_policy.py tests/test_memory_read_policy.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest tests/test_memory_retrieval_eval.py tests/test_memory_audit_api.py tests/test_memory_tool_boundary.py -q
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_evals.py --suite memory
```

Regression checks:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
git diff --check -- AGENTS.md docs src tests scripts
```

No test should require real provider keys, network memory service, vector DB, or stored real user data.

## Stop Criteria

Stop Phase2 after these are true:

- Candidate memory is audit-only by default.
- Explicit memory can update durable profile.
- Profile supersede is deterministic and tested.
- Recall excludes superseded/sensitive/expired memory from active context.
- Sensitive explicit memory requires confirmation.
- Recall report or equivalent prompt-safe metadata exists for debugging.
- Memory eval remains green.
- Memory tools remain thin.

Do not continue into Skill System, multi-agent memory sharing, vector retrieval, external memory platform, or learning loop inside Phase2.

## Phase3 Dependency Notes

Phase3 Skill v1 can depend on Phase2 only in this limited way:

- Skill may declare what memory retrieval intent it needs.
- Skill may receive recalled context through normal context injection.
- Skill may call governed `memory_save` / `memory_retrieval` only through ToolExecutor.
- Skill must not define its own memory schema, own profile merge, or bypass MemoryWritePolicy.

The output of Phase2 should make this possible without adding a second memory system.
