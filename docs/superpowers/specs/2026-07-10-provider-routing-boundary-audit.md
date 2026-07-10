# Provider Routing Boundary Audit

Date: 2026-07-10

## Purpose

This audit defines what "Phase 3 provider routing" means and why it should not
start from the current ProviderSpec cleanup. It also records which remaining
provider selectors can be centralized later without introducing a routing layer.

The goal is to keep the project local-first, preserve mock/local/offline
defaults, and avoid adding a provider router before there is a real worker or
task-specific routing requirement.

## Phase Relationship

Phase 1 established `ProviderSpec` as a fact source for selected provider
groups. It centralized provider names, env var names, defaults, adapter kind,
capabilities, and missing-env checks for chat, vision, and image generation.

Phase 2 moved existing readers toward `ResolvedProviderSpec`. Adapter factories
and config compatibility fields now read `ResolvedProviderSpec.adapter_kind` and
`ResolvedProviderSpec.capabilities` instead of maintaining parallel
provider-name dispatch facts.

Phase 3 is different. It is not another mechanical cleanup phase. It would be a
new routing design that decides which provider should handle a task, worker, or
auxiliary model call. That includes questions such as:

- whether the main assistant LLM and auxiliary worker LLM use different
  providers;
- whether summarization, planning, media analysis, or tool-specific tasks need
  separate provider choices;
- whether routing should account for cost, latency, model capability, fallback,
  or availability;
- how those decisions are traced, budgeted, and kept behind the existing
  `ActionValidator -> ToolExecutor -> ToolRegistry` boundary.

Because those needs are not yet concrete, Phase 3 should remain deferred.

## Current Selector Audit

| capability | current selector | current behavior | recommendation |
| --- | --- | --- | --- |
| `direct_chat` | `MULTIMODAL_AGENT_CHAT_PROVIDER` | Uses `ProviderSpec` and `ResolvedProviderSpec`. | Keep as-is. Do not add routing. |
| `image_understanding` | `MULTIMODAL_AGENT_VISION_PROVIDER` | Uses `ProviderSpec` and `ResolvedProviderSpec`. | Keep as-is. |
| `image_generation` | `MULTIMODAL_AGENT_IMAGE_PROVIDER` | Uses `ProviderSpec`; factories dispatch by adapter kind. | Keep as-is. |
| `web_search` | `MULTIMODAL_AGENT_SEARCH_PROVIDER` | Simple `mock` / `http` selector with HTTP adapter config in `ProviderConfig`. | Candidate for future mechanical ProviderSpec centralization, not Phase 3 routing. |
| `product_search` | `MULTIMODAL_AGENT_PRODUCT_PROVIDER` | `mock`, `local_json`, `http`, `haodanku`; local provider is allowed outside real-provider profiles. | Candidate for future mechanical ProviderSpec centralization. Preserve `local_json` offline behavior. |
| `price_compare` | `MULTIMODAL_AGENT_PRICE_PROVIDER` | `mock`, `local`, `http`, `haodanku`; `haodanku` shares config with product search. | Candidate for future mechanical ProviderSpec centralization. Preserve `local` offline behavior. |
| `render_3d` | `MULTIMODAL_AGENT_RENDER_PROVIDER` | `mock` / `http` skeleton; real provider remains opt-in and unimplemented. | Candidate for future mechanical ProviderSpec centralization. |
| `video_understanding` | `MULTIMODAL_AGENT_VIDEO_PROVIDER` | `mock`, `http`, `ark`; `ark` reuses Ark vision env names. | Candidate for future mechanical ProviderSpec centralization, but preserve Ark vision compatibility. |
| `vision_embedding` | `MULTIMODAL_AGENT_VISION_EMBEDDING_PROVIDER` | `mock` / `dashscope`; used by realtime semantic detection, not a normal ToolRegistry tool. | Leave out of the next cleanup unless realtime video work needs it. |
| memory remote backend | memory backend env vars | Owned by memory service policy and storage boundaries. | Do not move into tool ProviderSpec routing. |
| agent delegation / A2A | agent routing config and directory | Owned by `assistant_agent.agent_routing` and communication services. | Do not mix with provider routing. |

## Not Phase 3

The next safe cleanup, if desired, is a narrow ProviderSpec extension for
tool-facing service adapters:

- add specs for `web_search`, `product_search`, `price_compare`, `render_3d`,
  and `video_understanding`;
- keep the same env var names and selected provider strings;
- preserve local/offline exceptions such as `local_json` and `local`;
- let validation/readiness/factories read resolved specs;
- avoid adding fallback, priority, cost, latency, worker, or task routing.

This work should be named as a ProviderSpec completion or Phase 2.5 cleanup, not
Phase 3.

## Phase 3 Entry Criteria

Start Phase 3 only when at least one concrete routing requirement exists:

- a real auxiliary worker provider must differ from the main assistant provider;
- a tool or subtask needs a provider chosen dynamically from multiple eligible
  providers;
- trace/eval evidence shows a need for cost, latency, capability, or fallback
  routing;
- agent delegation or A2A introduces provider choice that cannot be represented
  as existing adapter configuration.

Before implementation, Phase 3 must define:

- the routing input contract;
- the allowed provider groups and capability facts;
- how routing decisions are traced and budgeted;
- how mock/local/offline defaults remain deterministic;
- how routing stays behind existing runtime and tool governance boundaries.

## Recommended Next Step

Do not implement Phase 3 now.

If more cleanup is useful, write a separate small plan for ProviderSpec
completion over `web_search`, `product_search`, `price_compare`, `render_3d`,
and `video_understanding`. That plan should be test-first and must prove that no
runtime profile gate, env var name, missing-env behavior, or mock/local default
changes.
