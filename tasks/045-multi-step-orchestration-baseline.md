# Task 045 Multi-step Orchestration Baseline

## Goal

确保多步用户请求能被规划为多个 capability 顺序执行，而不是误判为单一 intent。

## Read first

- `docs/42-assistant-capability-routing-baseline.md`
- 当前 planner
- 当前 LangGraph loop
- 当前 tool executor

## Requirements

覆盖多步场景：

```text
找这张图里的鞋子，比较价格，再生成海报
根据上次那个包，生成一张宣传图
帮我找 500 元以内的白鞋，再比较价格
用这个视频里的商品做一个 3D 展示
```

## Expected patterns

```text
image_understanding → product_search → price_compare → image_generation
memory_retrieval → image_generation
product_search → price_compare
video_understanding → render_3d
```

## Requirements

- planner 输出明确 plan steps。
- graph loop 按步骤执行。
- 每步结果可被后续步骤读取。
- 失败时遵守 RecoveryPolicy。
- 默认 mock-only。

## Tests

新增或更新：

```text
tests/test_assistant_multistep_orchestration.py
```

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 046。
