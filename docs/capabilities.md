# Capabilities

The assistant routes user intent to existing capabilities.

## Capability List

| Capability | Purpose | Default Provider |
| --- | --- | --- |
| `direct_chat` | Text-only assistant response | mock |
| `image_generation` | Generate image result from text | mock/local |
| `image_understanding` | Understand image refs | mock, optional real vision |
| `video_understanding` | Understand video refs through external-style adapter | mock |
| `product_search` | Search product candidates | mock/local JSON |
| `price_compare` | Compare candidate prices | mock/local |
| `render_3d` | Produce render preview result | mock |
| `memory_retrieval` | Retrieve local memory context | in-memory/jsonl |
| `multi_step_orchestration` | Chain capabilities based on intent | graph runtime |

## Output Contract

Tool results are structured and include capability-specific data, errors, metadata, and output references where relevant.

## Boundaries

- Product search is not a crawler or commerce platform.
- Render is not a production rendering platform.
- Video understanding is not a video model engineering system.
- Real Providers are not default.
