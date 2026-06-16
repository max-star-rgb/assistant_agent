# 89 Phase 5G Video Understanding Review Checklist

## 必须满足

- `video_understanding` 作为 Assistant capability 已接入。
- VideoUnderstandingRequest / VideoUnderstandingResult contract 稳定。
- VideoUnderstandingAdapter contract 明确。
- 默认 provider 为 mock。
- 可选 HTTP / external Video MLLM skeleton 存在或边界明确。
- 缺 video_ref 时进入 ask_followup。
- 有 video 但普通聊天时不强制 video_understanding。
- video_understanding → product_search 可运行。
- video_understanding → price_compare 可运行。
- video_understanding → image_generation 可运行。
- video_understanding → render_3d 可运行。
- smoke 脚本默认 mock 可运行。
- eval / API / WebSocket / demo runner 有视频覆盖。
- 默认 pytest 不调用真实 Video Provider。
- 默认 eval 不调用真实 Video Provider。
- 不提交真实视频。
- 不输出 API Key、Authorization、Bearer token 或完整 base64。

## 审计报告

最终生成：

```text
docs/90-phase5g-video-understanding-review.md
```

报告包含：

1. Video Understanding Capability 状态。
2. Request / Result / Adapter contract。
3. Mock / HTTP / external Provider 边界。
4. 多步链路状态。
5. Smoke 能力。
6. Eval / API / WebSocket / Demo 覆盖。
7. 安全边界。
8. Phase 5H 建议。

## Phase 5H 建议方向

Phase 5G 后可以考虑：

```text
Provider Safety / Retry / Cost / Trace Query
Memory Hardening
MCP / Skills Packaging
```

不要在 Phase 5G 审计中直接实现这些内容。
