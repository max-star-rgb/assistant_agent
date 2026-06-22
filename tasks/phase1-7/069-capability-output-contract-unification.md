# Task 069 Capability Output Contract Unification

## Goal

为核心 capability 输出增加统一 contract 字段，让 response composer 和 demo runner 稳定读取结果。

## Read first

- `docs/70-capability-output-contract-unification.md`
- 当前 ToolResult schema
- 当前 response composer
- 当前 API routes
- 当前 tool implementations

## Requirements

- 定义或扩展 CapabilityOutputContract schema。
- 核心 capability 至少能输出 contract：
  - direct_chat
  - image_generation
  - image_understanding
  - video_understanding
  - product_search
  - price_compare
  - render_3d
  - memory_retrieval
- 保留旧字段兼容。
- API 可返回 contract。
- WebSocket 可返回 contract 摘要。
- 不暴露 provider raw response。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_capability_output_contract.py
tests/test_api_capability_contract.py
```

覆盖：

- 单个 capability contract。
- 多步 capability contract。
- errors 结构。
- API 输出 contract。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 070。
