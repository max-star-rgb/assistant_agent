# Prompt Engineering Authority And Skill Routing Design

**Date:** 2026-07-13

**Status:** Approved direction; implementation pending written-spec review

## 1. Goal

Create a stable repository authority for prompt engineering and make Codex read it automatically for the right tasks, without creating an overlapping prompt-specific skill.

The change will:

- add `docs/prompt-engineering-architecture.md` as the long-lived prompt-engineering authority;
- keep `.codex/skills/assistant-agent-context-engineering` as the shared workflow entry for prompt and context work;
- add explicit conditional routing in that skill so prompt work reads the prompt authority and context work reads the context authority;
- split the prompt-engineering scope from the context-engineering scope in `AGENTS.md` while pointing both scopes at the same workflow skill;
- reduce `docs/CONTEXT_ENGINEERING_STATUS.md` to a concise cross-boundary pointer for prompt compilation instead of duplicating prompt architecture.

This is an information-architecture and agent-routing change. It does not change runtime prompt text, Provider requests, tool permissions, memory policy, context budgets, or user-visible behavior.

## 2. Current Problem

The repository already routes `prompt/context rendering` work through `.codex/skills/assistant-agent-context-engineering`, and `docs/CONTEXT_ENGINEERING_STATUS.md` contains a short Prompt Rendering section. The production request path now also has a concrete `PromptCompiler` boundary.

However, the current arrangement has three weaknesses:

1. `docs/CONTEXT_ENGINEERING_STATUS.md` owns conversation history, memory injection, compaction, context budgets, observability, and prompt rendering. Prompt engineering is becoming a distinct concern inside an already broad authority document.
2. `docs/superpowers/specs/2026-07-13-prompt-compiler-design.md` records the implementation-stage design, but a dated design spec is not the long-lived repository authority for future prompt changes.
3. `AGENTS.md` and the context-engineering skill can trigger the correct broad workflow, but they do not tell Codex when it must read a prompt-specific authority covering profiles, compilation modes, message layout, final-only behavior, and future prompt modules.

The result is a risk that future work updates one runtime call site, one system-prompt profile, or one Markdown prompt module without checking the complete provider-request contract.

## 3. Considered Approaches

### 3.1 Recommended: Dedicated Authority, Existing Skill

Add a prompt-engineering authority document and teach the existing context-engineering skill to route conditionally.

Benefits:

- establishes a clear long-lived source of truth;
- avoids two overlapping skills firing for the same change;
- keeps prompt/context cross-boundary tasks in one workflow;
- makes the smallest repository-level change consistent with the current architecture.

Trade-off:

- the context-engineering skill remains a shared entry and needs a clear routing section.

### 3.2 Dedicated Authority And Dedicated Prompt Skill

Add both `docs/prompt-engineering-architecture.md` and `.codex/skills/assistant-agent-prompt-engineering`.

Benefits:

- gives prompt work a fully independent workflow;
- could later support prompt versioning, experiment rollout, and eval-specific procedures.

Trade-offs:

- overlaps strongly with context rendering and provider request assembly;
- creates ambiguous dual-skill triggers for PromptCompiler and renderer changes;
- adds maintenance before prompt engineering has an independent release lifecycle.

This remains a future option if prompt work gains its own sustained experiment, evaluation, and release process.

### 3.3 Expand Context Engineering Status Only

Keep all prompt architecture in `docs/CONTEXT_ENGINEERING_STATUS.md`.

Benefit:

- minimal file and routing changes.

Trade-offs:

- continues mixing context collection with provider request compilation;
- makes the status document less scannable;
- weakens ownership for persona, spoken style, final-only, and prompt-module evolution.

This approach is rejected.

## 4. Authority Model

The repository will use the following ownership model:

| Concern | Long-lived authority |
| --- | --- |
| System prompt profiles, prompt options, PromptCompiler, compilation modes, renderers, provider message layout, final-only behavior, prompt modules, prompt tests and eval gates | `docs/prompt-engineering-architecture.md` |
| Conversation history, session summary, memory context injection, realtime task state as context data, compaction, context budgets and context reports | `docs/CONTEXT_ENGINEERING_STATUS.md` |
| Tool visibility, ToolSpec, ActionValidator, ToolExecutor, registry, execution allowlists, side-effect policy and tool retry/budget | `docs/tool-calling-architecture.md` |
| Long-term memory retrieval, ranking, read/write policy, retention and audit | `docs/memory-service-architecture.md` |
| Gateway protocol frames, run/session lifecycle, cancel/interrupt and transport behavior | `docs/gateway-architecture.md` |

Cross-boundary rule:

- context engineering determines what prompt-safe data is available in `AssistantContextPack`;
- prompt engineering determines how that prepared data becomes a Provider-facing `ChatRequest`;
- tool calling determines which tools may be exposed or executed and how returned tool calls are governed;
- PromptCompiler may consume governed tool subsets but does not own tool policy;
- prompt documentation may describe these interfaces but must link to, not duplicate, the owning authority.

When two authority documents appear to conflict, the document owning the behavior wins. `AGENTS.md` remains the repository workflow entry and does not duplicate the detailed architecture.

## 5. New Prompt Engineering Authority

Create `docs/prompt-engineering-architecture.md` as a current-state architecture document, not a roadmap or implementation diary.

It will contain the following sections.

### 5.1 New Conversation Handoff

A short top section will state:

- production Provider requests use the unified `PromptCompiler` path;
- the compiler is deterministic and side-effect free;
- `SystemPromptProfile` controls instruction content while `PromptCompileMode` controls request structure and tool availability;
- legacy prompt-json rendering is offline/test compatibility only;
- prompt work must preserve tool, memory, context, and Provider boundaries;
- the current known prompt source files and focused tests.

### 5.2 Scope And Terminology

Define:

- prompt engineering;
- system instruction/profile/options;
- rendered context;
- prompt compilation;
- provider-native tool messages;
- final-only modes;
- prompt module versus repository workflow skill.

The document will explicitly distinguish `.codex/skills/**` workflow instructions from runtime `skills/<skill_id>/SKILL.md` prompt-safe business capability descriptors.

### 5.3 Current Compilation Pipeline

Document the production flow:

```text
UserRequest + session/context/memory/tool evidence
  -> AssistantContextPack
  -> trusted SystemPromptProfile + SystemPromptOptions
  -> PromptCompileMode
  -> PromptCompiler.compile(...)
  -> ChatRequest(messages, tools, tool_choice, generation options)
  -> ChatAdapter / Provider
```

Also document that `PromptCompileResult` supplies the actual system instruction, rendered context, and selected tool specs to prompt-safe observability.

### 5.4 Profiles And Compilation Modes

Record the current profiles:

- `TEXT_DEFAULT`;
- `REALTIME_PHONE`;
- `FINAL_ONLY`.

Record the current modes and invariants:

| Mode | Context renderer | Native tool evidence | Provider tools | `tool_choice` |
| --- | --- | --- | --- | --- |
| `NATIVE_TOOL` | native context | included when present | governed prompt subset | `auto` |
| `NATIVE_FINAL_ONLY` | native context | included | empty | `none` |
| `SUMMARY_FINAL_ONLY` | final-only summary prompt | not emitted as native pairs | empty | `None` |

The authority will state that profile and mode are separate axes. Supported runtime pairings are explicit and must be tested when changed.

### 5.5 Message And Tool-Schema Invariants

Document the native message order:

```text
system
user
assistant tool_call #1
tool result #1
assistant tool_call #2
tool result #2
...
```

Document these invariants:

- tool call/result IDs and names must remain paired;
- raw Provider call payloads are preserved when valid;
- fallback IDs are deterministic;
- observations use prompt-safe copies prepared by upstream context/tool boundaries;
- tool schemas are sent through the Provider-native tools field and are not duplicated into the native user message;
- final-only modes cannot regain tools from an upstream non-empty list;
- generation parameters and stream callbacks remain explicit compiler inputs;
- production call sites must not assemble `ChatRequest` prompt messages independently of PromptCompiler.

### 5.6 Trust And Governance Boundaries

The authority will state:

- conversation, memory, realtime task state, observations, and tool outputs are untrusted context data, not system instructions;
- current user input and fresh governed tool evidence take precedence over retrieved memory when they conflict;
- PromptCompiler does not retrieve memory, query stores, access ToolRegistry, execute tools, call Providers, mutate runtime state, or write trace events;
- runtime tool calls still pass through ActionValidator, ToolExecutor, ToolRegistry, policy, and audit boundaries;
- prompt files and runtime skill descriptors cannot create a bypass around tool governance.

### 5.7 Future Markdown Prompt Modules

The authority will define the extension boundary without introducing a loader in this change:

```text
versioned Markdown/config module
  -> trusted parse/validation layer
  -> SystemPromptProfile or resolved prompt material
  -> PromptCompiler
```

Future `persona.md`, `spoken_style.md`, or channel-specific modules must not make PromptCompiler read directories, retrieve memory, select tools, or call Providers. Module precedence, validation, versioning, and token budgets require a separate approved behavior change before implementation.

### 5.8 Change Workflow And Validation

Classify prompt changes by risk:

- documentation-only routing changes;
- behavior-preserving request assembly refactors;
- model-visible instruction changes;
- tool exposure or final-only permission changes;
- Provider-specific message-format changes.

For each behavior change, require focused tests for the affected profile/mode, message ordering, selected tools, tool choice, fallback IDs, generation options, and final-only behavior. Model-visible prompt changes additionally require representative eval cases; real Provider smoke remains explicit opt-in.

### 5.9 Known Limitations

Record current limitations without turning them into an active roadmap:

- system prompt content remains code-defined;
- there is no runtime Markdown prompt-module loader;
- prompt versioning and A/B rollout are not implemented;
- global context control is not fully token-hard-limited;
- PromptCompileRequest and AssistantContextPack carry related observation inputs that callers must keep consistent;
- Provider-specific prompt adapters still consume the shared `ChatRequest` contract.

## 6. Skill Routing Design

Modify `.codex/skills/assistant-agent-context-engineering/SKILL.md` rather than adding a new skill.

Add a `Task Routing` section immediately after the repository start checks.

### 6.1 Prompt Route

The skill must read `docs/prompt-engineering-architecture.md` for tasks involving any of:

- `PromptCompiler`, `PromptCompileRequest`, `PromptCompileResult`, or `PromptCompileMode`;
- `SystemPromptProfile`, `SystemPromptOptions`, or system instruction rendering;
- prompt/context renderers when their Provider-visible output changes;
- native system/user/assistant/tool message ordering;
- Provider `ChatRequest` prompt assembly, tools exposure, or `tool_choice`;
- text, realtime-phone, or final-only prompt behavior;
- persona, spoken style, prompt modules, prompt versioning, prompt experiments, or prompt evals.

### 6.2 Context Route

The skill must read `docs/CONTEXT_ENGINEERING_STATUS.md` for tasks involving:

- conversation history or session summaries;
- memory context injection as prompt data;
- realtime task-state context;
- context compaction or truncation;
- context/token budgets;
- context reports and section accounting;
- construction of `AssistantContextPack`.

### 6.3 Combined Route

Read both authorities when a task changes what enters `AssistantContextPack` and how that data is rendered or compiled into `ChatRequest`.

Read the tool-calling or memory skill and authority in addition when a task changes the owning tool or memory behavior. The context skill must not reinterpret those policies.

### 6.4 Skill Source Map And Validation

Extend the skill source map with:

- `src/assistant_agent/agent/system_prompt_policy.py`;
- `src/assistant_agent/services/context/prompt_compiler.py`;
- `src/assistant_agent/services/context/renderer.py`;
- production compiler call sites in `assistant_loop_nodes.py` and `runtime.py`;
- `tests/test_prompt_compiler.py` and native runtime prompt-policy tests.

Extend validation paths to include `docs/prompt-engineering-architecture.md`. The skill remains concise and routes to the authority instead of reproducing its contents.

## 7. AGENTS.md Routing Design

Split the current combined context/prompt row into two rows that reuse the same skill:

| scope | entry |
| --- | --- |
| prompt engineering, `PromptCompiler`, system prompt profiles/options, prompt renderers, persona/spoken style, final-only behavior, Provider `ChatRequest` message/tool assembly | `.codex/skills/assistant-agent-context-engineering`; `docs/prompt-engineering-architecture.md` |
| context engineering, conversation history, memory context injection, realtime task-state context, compaction, context budget and report | `.codex/skills/assistant-agent-context-engineering`; `docs/CONTEXT_ENGINEERING_STATUS.md` |

Also add `docs/prompt-engineering-architecture.md` to the repository's authority-document list in the documentation section.

`AGENTS.md` must remain a short routing index. It will not copy profile tables, compile-mode behavior, invariants, or validation details.

## 8. Context Authority Migration

Update `docs/CONTEXT_ENGINEERING_STATUS.md` only enough to establish the new ownership boundary:

- keep the statement that `AssistantContextPack` supplies production prompt inputs;
- keep context trust labeling, compaction, budget, and context-report behavior;
- replace detailed PromptCompiler/profile/mode ownership with a concise pointer to `docs/prompt-engineering-architecture.md`;
- avoid duplicating the prompt mode table or message assembly rules;
- retain cross-boundary details when they describe context preparation rather than prompt compilation.

The dated PromptCompiler design and plan remain implementation evidence. They are not linked from `AGENTS.md` as current authority and do not override the new architecture document.

## 9. Error Handling And Consistency

This change introduces no runtime error path. Documentation consistency is enforced by:

- explicit ownership and cross-document precedence;
- conditional skill reading rules;
- direct links from `AGENTS.md`, the context skill, and the context status document;
- no duplicated profile/mode contract outside the prompt authority except short summaries needed at boundaries;
- a repository search during validation to detect stale claims that the context status document is the sole prompt authority.

## 10. Validation Plan

Implementation validation will include:

```bash
rg -n "prompt-engineering-architecture|PromptCompiler|prompt engineering" \
  AGENTS.md .codex/skills/assistant-agent-context-engineering/SKILL.md \
  docs/CONTEXT_ENGINEERING_STATUS.md docs/prompt-engineering-architecture.md

git diff --check -- \
  AGENTS.md \
  .codex/skills/assistant-agent-context-engineering/SKILL.md \
  docs/CONTEXT_ENGINEERING_STATUS.md \
  docs/prompt-engineering-architecture.md \
  docs/superpowers/specs/2026-07-13-prompt-engineering-authority-routing-design.md
```

Because the implementation is documentation and workflow routing only, it does not require runtime code changes or real Provider calls. Existing fast tests may be run as a regression guard, but no runtime behavior claim will be based on documentation checks alone.

## 11. Acceptance Criteria

The work is complete when:

1. `docs/prompt-engineering-architecture.md` exists and describes current production prompt architecture, boundaries, invariants, extension rules, and validation expectations.
2. `AGENTS.md` has separate prompt and context routing rows and lists the new authority document.
3. The existing context-engineering skill conditionally reads the prompt authority, context authority, or both based on task scope.
4. The context status document points to the prompt authority and no longer acts as the detailed owner of Provider request compilation.
5. No new prompt-specific skill is added.
6. No runtime source, prompt text, tools, memory policy, Provider configuration, or user-visible behavior changes.
7. Repository searches and diff checks pass.
8. The design document, authority document, AGENTS update, and skill update are committed together after implementation validation, in accordance with repository commit policy.

## 12. Files In The Implementation Scope

Create:

- `docs/prompt-engineering-architecture.md`

Modify:

- `AGENTS.md`
- `.codex/skills/assistant-agent-context-engineering/SKILL.md`
- `docs/CONTEXT_ENGINEERING_STATUS.md`

Retain as design evidence:

- `docs/superpowers/specs/2026-07-13-prompt-engineering-authority-routing-design.md`

No `src/**` or `tests/**` files are part of this implementation.
