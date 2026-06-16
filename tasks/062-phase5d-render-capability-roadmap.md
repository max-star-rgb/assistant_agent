# Task 062 Phase 5D Render Capability Roadmap

## Goal

确认 Phase 5D 只做轻量 render_3d capability baseline，不做独立渲染平台。

## Read first

- `docs/62-phase5d-render-capability-roadmap.md`
- `docs/61-phase5c-product-search-price-compare-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5D 目标为 Render / 3D 渲染能力基线。
- 明确 render_3d 是 Assistant capability，不是渲染平台。
- 明确不接入真实 Blender / Unity / Three.js。
- 明确默认 mock。
- 明确不做复杂任务队列、材质系统、模型资产管理。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/62-phase5d-render-capability-roadmap.md
tasks/README_PHASE5D.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 063。
