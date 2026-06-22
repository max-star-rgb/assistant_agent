# Task 082 Video Understanding Request / Result / Adapter Baseline

## Goal

定义 video_understanding 的最小 Request / Result / Adapter contract，并保证默认 mock 可用。

## Read first

- `docs/85-video-understanding-contract.md`
- 当前 vision/video tool / adapter 结构
- 当前 schemas
- 当前 ToolRegistry
- 当前 CapabilityOutputContract

## Requirements

- 定义或检查 VideoUnderstandingRequest schema。
- 定义或检查 VideoUnderstandingResult schema。
- 定义 VideoUnderstandingAdapter Protocol。
- 新增或修正 MockVideoUnderstandingAdapter。
- 新增或修正 VideoUnderstandingTool。
- 默认使用 MockVideoUnderstandingAdapter。
- Tool 不直接调用 HTTP / SDK。
- 输出 contract 包含 capability=video_understanding。
- 不调用真实 Provider。

## Tests

新增或更新：

```text
tests/test_video_understanding_adapter_contract.py
tests/test_video_understanding_tool.py
tests/test_video_understanding_contract.py
```

覆盖：

- mock default。
- video_ref request。
- missing video_ref error。
- output_ref 稳定。
- capability contract。
- no real provider call。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 083。
