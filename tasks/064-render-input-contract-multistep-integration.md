# Task 064 Render Input Contract and Multistep Integration

## Goal

让 render_3d 可以接收来自文本、商品搜索、图片理解、视频理解和记忆的上下文。

## Read first

- `docs/64-render-input-and-multistep-design.md`
- 当前 planner
- 当前 tool_input_builder
- 当前 LangGraph loop
- 当前 ProductResult / VisionResult / MemoryResult schema

## Requirements

支持以下链路：

```text
text → render_3d
product_search → render_3d
image_understanding → render_3d
video_understanding → render_3d
memory_retrieval → render_3d
```

要求：

- planner 能输出 render_3d plan step。
- tool_input_builder 能构造 RenderRequest。
- 上游结果可注入 render request。
- graph loop 把 render_3d 当普通 step 执行。
- 默认 mock-only。
- 不调用真实渲染服务。

## Tests

新增或更新：

```text
tests/test_render_multistep_integration.py
tests/test_render_tool_input_builder.py
```

覆盖：

- text → render。
- search → render。
- image understanding → render。
- video understanding → render。
- memory → render。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 065。
