# Tool Qualification and Identity Recall Design

Date: 2026-07-13

## Objective

Make the production tool assembly path consistent with the project rule that the LLM is the only task reasoner. The system defines the legal action space from structured runtime facts and enforces execution safety, but it does not infer user intent to decide which qualified tools the LLM may see.

Keep a real recall boundary for future context-budget pressure. The current recall implementation is an identity operation and must not remove qualified tools based on request text.

## Design Principles

- The LLM is the only task reasoner, but it is not the permission authority.
- Qualification is deterministic governance. Recall is optional context optimization.
- User text does not activate tools, toolsets, or skills during qualification.
- Risk controls execution mode; risk alone does not hide an otherwise qualified tool.
- Provider-native tool calls remain behind `ActionValidator -> ToolExecutor -> ToolRegistry`.
- Prefer pure functions and data contracts over strategy interfaces, factories, or plugin frameworks.

## Run-Scoped Tool Sets

`RunToolSet` records four prompt-safe sets:

```text
registered_tool_names
qualified_tool_names
exposed_tool_names
executable_tool_names
```

Their meanings are:

- `registered`: tool instances owned by the current runtime registry.
- `qualified`: registered tools that satisfy structured environment, visibility, permission, toolset, and explicit skill policy.
- `exposed`: qualified tools whose ToolSpec is sent to the LLM for the current turn.
- `executable`: the local execution allowlist consumed by `ActionValidator`.

The model must preserve these invariants:

```text
qualified ⊆ registered
exposed ⊆ qualified
executable ⊆ qualified
```

The current provider-native production path uses identity recall, so:

```text
qualified = exposed = executable
```

The model does not require `executable ⊆ exposed`, leaving room for deterministic non-model entry paths. This design removes the current mock plan allowlist widening because default mock plan tools are already qualified and exposed.

## Assembly Pipeline

The assembly path is:

```text
ToolRegistry.list_specs()
  -> qualify_tool_specs()
  -> recall_qualified_tool_specs()
  -> RunToolSet
  -> PromptCompiler / provider-native tools
```

### Qualification

`qualify_tool_specs()` may consume only structured governance facts:

- `ToolSpec` policy metadata;
- declared `requires_env` prerequisites;
- `enabled_by_default`;
- explicit `request.metadata.tool_visibility.enabled_tools`;
- explicit `request.metadata.tool_visibility.enabled_toolsets`;
- explicit `request.metadata.tool_visibility.enabled_skills`;
- valid skill descriptors with matching `tool:<name>` permissions;
- channel or structured request constraints when they are true capability requirements.

Runtime profile and provider capability selection remain upstream concerns of registry and adapter construction in this phase; qualification does not create a second provider-resolution table. Visibility overrides are trusted entry metadata supplied by the runtime or application, not values proposed by the model.

It must not use:

- request-text keywords;
- `_has_*_intent()` helpers;
- substantive-task classification;
- skill tag matching against request text;
- a local intent, router, or plan decision.

Qualification produces structured hard exclusions such as:

```yaml
private.lookup:
  - missing_required_env:PRIVATE_KEY
calendar.write:
  - disabled_by_default
admin.tool:
  - skill_activation_required
```

There is no live remote health probe in qualification. Provider availability continues to follow explicit runtime profile and adapter configuration, and execution failures remain structured `ToolResult` values.

### Explicit Skill Activation

Only `enabled_skills` explicitly activates a repo skill for qualification. A valid activated skill must be enabled, allow model invocation, govern the tool, and declare the matching `tool:<name>` permission.

Request text and visibility tags do not activate skills. Ordinary non-`skill_only` tools remain governed by their own visibility policy and do not require a skill merely because a skill descriptor mentions them.

Prompt-safe capability descriptors are exposed only for explicitly active skills whose governed tools are qualified.

### Identity Recall

`recall_qualified_tool_specs(request, qualified_specs)` is a pure function boundary. The current implementation returns the input ToolSpecs in the original order and records `recall_identity`.

It may accept the request so a future implementation can account for context budget, but the identity implementation must not inspect request text to remove, reorder, or activate tools.

No `ToolRecallStrategy`, factory, protocol, embedding index, or meta-tool is introduced in this phase. Future semantic recall requires a separate approved design with high-recall behavior and a recovery path for omitted qualified tools.

Identity recall has no full-list fallback distinction:

- an empty qualified set produces an empty exposed set;
- a non-empty qualified set is exposed unchanged;
- hard qualification exclusions are never restored by recall.

## Risk and Execution

Risk metadata does not make an otherwise qualified tool invisible. It determines how an accepted call is handled:

| Risk behavior | Runtime handling |
| --- | --- |
| pure / local read / external read | automatic gate when other policies allow |
| compensatable | soft gate and idempotency handling |
| write or confirmation-sensitive | hard gate and confirmation handling |
| unknown | conservative hard gate |

High-risk tools may still require explicit toolset, skill, user, tenant, or profile authorization to become qualified. Natural-language intent does not grant that authorization.

`ActionValidator` continues to enforce `RunToolSet.executable_tool_names`, semantic safety checks that protect sensitive operations, and input schema validation. `ToolExecutor` continues to own identity binding, cancellation, risk gates, confirmation, idempotency, budget, retry, history, events, trace, and structured failures.

## Compatibility Boundaries

- Provider-native adapters receive every qualified ToolSpec in the current identity-recall phase.
- A governed empty tool set never falls back to the full registry.
- Mock/local/offline defaults remain deterministic and do not enable real providers.
- The mock plan execution allowlist widening is removed; qualified default tools already satisfy deterministic plans.
- MCP `tool_run` and local tools CLI keep their explicit entry allowlists and still enter `ActionValidator -> ToolExecutor`; they do not fabricate a model-facing `RunToolSet`.
- Existing tool-specific validators, dependency scheduling, observation compaction, and executor governance remain in place.

## Source Changes

The implementation is expected to update:

- `src/assistant_agent/schemas/tools.py`: rename `available_tool_names` to `qualified_tool_names` and enforce set invariants.
- `src/assistant_agent/services/context/tool_catalog.py`: separate qualification from identity recall and remove text-intent selection from production assembly.
- `src/assistant_agent/services/context/builder.py`: consume qualified and recalled ToolSpecs.
- `src/assistant_agent/services/context/capability_catalog.py`: restrict skill descriptor exposure to explicitly active qualified skills.
- `src/assistant_agent/agent/assistant_loop_nodes.py`: remove mock plan executable widening.
- prompt compiler, renderer, report, state, and validator consumers where field or reporting semantics change.
- authoritative architecture and context-engineering documentation.

No new dependency is required.

## Test Design

Implementation follows TDD. Tests must first demonstrate the old behavior and fail for the intended reason.

Required cases:

1. Semantically different request texts with identical structured policy inputs produce identical qualified, exposed, and executable sets.
2. Request text matching a skill tag does not activate a skill-only tool.
3. Explicit `enabled_skills` with a valid matching permission qualifies the skill-only tool.
4. Missing environment prerequisites, default-disabled tools, and missing skill activation remain hard exclusions.
5. Read-only, compensatable, and high-risk tools with equal qualification facts are all exposed; their policy views retain distinct runtime gates.
6. Identity recall preserves qualified ToolSpec order and records `recall_identity`.
7. Provider-native `ChatRequest.tools` contains every qualified ToolSpec.
8. Validator rejection coverage uses a tool that failed qualification, not a tool omitted by semantic recall.
9. A governed empty qualified set does not fall back to the registry.
10. Existing native tool execution, skill, memory-media, calendar, mock/offline, prompt compiler, renderer, and context-report tests remain compatible after their old prompt-subset assumptions are updated.

Targeted tests run before the full suite. Full-suite failures unrelated to this change are reported separately with exact evidence.

## Out of Scope

- Semantic or embedding-based tool recall.
- A `tool_search` or capability-expansion meta-tool.
- Live provider health probing during context assembly.
- New permission storage or user/tenant authorization services.
- A recall strategy interface or plugin framework.
- Replacing existing tool-specific safety validation, confirmation, or executor risk gates.

## Acceptance Criteria

- Production tool qualification does not read or classify request text.
- Request text cannot activate a tool or skill.
- All qualified tools are exposed and executable in provider-native identity-recall mode.
- Risk continues to affect execution handling without acting as semantic exposure selection.
- `RunToolSet` reports registered, qualified, exposed, and executable sets with prompt-safe exclusion reasons.
- Recall exists as a tested pure-function boundary and currently behaves as identity.
- No provider, validator, executor, MCP, or mock path bypasses the established governance chain.
