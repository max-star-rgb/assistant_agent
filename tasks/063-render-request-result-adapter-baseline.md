# Task 063 Render Request / Result / Adapter Baseline

## Goal

定义 render_3d 的最小 RenderRequest / RenderResult / RenderAdapter contract。

## Read first

- `docs/63-render-capability-contract.md`
- 当前 render tool / adapter
- 当前 ProviderConfig
- 当前 ToolRegistry

## Requirements

- 检查或定义 RenderRequest schema。
- 检查或定义 RenderResult schema。
- 检查或定义 RenderAdapter Protocol。
- 默认使用 MockRenderAdapter。
- 可预留 HttpRenderAdapter skeleton，但不默认启用。
- 缺配置返回 provider_unconfigured。
- Tool 不直接调用 HTTP / SDK。
- 不调用真实渲染服务。

## Tests

新增或更新：

```text
tests/test_render_adapter_contract.py
tests/test_render_provider_selection.py
tests/test_render_tool.py
```

覆盖：

- mock default。
- text-only render request。
- http provider 缺配置。
- render_missing_scene。
- output_ref 稳定。
- 不调用真实 Provider。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 064。
