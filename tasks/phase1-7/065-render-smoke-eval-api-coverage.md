# Task 065 Render Smoke / Eval / API Coverage

## Goal

为 render_3d 增加 smoke、eval 和 API 覆盖。

## Read first

- `docs/65-render-smoke-eval-api-plan.md`
- 当前 scripts/
- 当前 tests/evals/
- 当前 API routes
- 当前 WebSocket event tests

## Requirements

新增或更新：

```text
scripts/smoke_render_3d.py
tests/test_render_smoke_script.py
tests/test_render_api.py
tests/test_render_evals.py
```

要求：

- smoke 脚本 import 不触发 Provider。
- 默认 mock smoke 可运行。
- http provider 缺配置时清晰提示。
- 默认 eval 不调用真实 Provider。
- API 返回 render contract。
- WebSocket 可观察 render tool event。
- 不提交真实渲染结果。

## Eval cases

至少覆盖：

```text
text_only_render
product_search_to_render
image_understanding_to_render
video_understanding_to_render
memory_to_render
```

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 066。
