# Task 042 Intent Taxonomy and Capability Contracts

## Goal

统一 Assistant Agent 的 intent taxonomy 和 capability contracts。

## Read first

- `docs/42-assistant-capability-routing-baseline.md`
- `docs/43-direct-chat-and-text-only-capabilities.md`
- 当前 `src/multimodal_agent/agent/intent.py`
- 当前 `src/multimodal_agent/agent/router.py`
- 当前 schema/tool definitions

## Required intents

至少支持：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
multi_step_orchestration
ask_followup
```

## Requirements

- 检查当前 intent 命名是否一致。
- 若存在 `understand_image` 等旧命名，决定是否保留 alias 或迁移到 `image_understanding`。
- 定义每个 intent 的输入要求。
- 定义每个 capability 的输出 contract。
- Tool Registry 与 intent router 命名保持一致。
- 不默认调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_intent_taxonomy.py
tests/test_capability_contracts.py
```

覆盖：

- 每个 intent 可被识别或路由。
- 每个 capability 有明确 contract。
- alias 不破坏历史测试。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 043。
