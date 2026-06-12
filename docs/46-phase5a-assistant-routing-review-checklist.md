# 46 Phase 5A Assistant Routing Review Checklist

## 必须满足

- Phase 5A 主线已从 Vision-only 改为 Assistant Capability Routing。
- direct_chat 支持纯文本输入。
- image_generation 支持纯文本输入。
- product_search 支持纯文本输入。
- price_compare 支持纯文本输入或搜索结果输入。
- render_3d 支持纯文本场景描述。
- image_understanding 只在需要看图时触发。
- video_understanding 只在需要看视频时触发。
- 有媒体输入时，文本意图仍然优先。
- 多步任务能触发多个 capability。
- 歧义输入能进入 followup。
- eval 覆盖所有 capability routing。
- 默认 pytest / eval 不调用真实 Provider。

## 审计报告

最终生成：

```text
docs/47-phase5a-assistant-routing-review.md
```

报告包含：

1. Assistant Agent 定位。
2. Capability matrix。
3. Text-only 能力状态。
4. Media-aware routing 状态。
5. Multi-step routing 状态。
6. Eval 覆盖情况。
7. Vision Provider validation 的降级定位。
8. 下一阶段建议。
