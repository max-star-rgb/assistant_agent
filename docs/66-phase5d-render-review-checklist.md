# 66 Phase 5D Render Review Checklist

## 必须满足

- `render_3d` 作为 Assistant capability 已接入。
- 纯文本场景描述可触发 render_3d。
- product_search 结果可传给 render_3d。
- image_understanding 结果可传给 render_3d。
- video_understanding 结果可传给 render_3d。
- memory_retrieval 结果可传给 render_3d。
- RenderRequest / RenderResult contract 稳定。
- RenderAdapter contract 明确。
- 默认 provider 仍为 mock。
- 默认 pytest 不调用真实渲染服务。
- 默认 eval 不调用真实渲染服务。
- smoke 脚本只有用户显式运行才触发真实 Provider。
- 渲染产物目录被 `.gitignore` 忽略。
- 不做 Blender / Unity / Three.js 生产级接入。
- 不做材质系统、模型资产平台或复杂渲染队列。

## 审计报告

最终生成：

```text
docs/67-phase5d-render-capability-review.md
```

报告包含：

1. Render Capability 状态。
2. RenderRequest / RenderResult contract。
3. Mock / HTTP Provider 边界。
4. 多步链路状态。
5. Smoke 能力。
6. Eval / API 覆盖。
7. 安全边界。
8. Phase 5E 建议。
