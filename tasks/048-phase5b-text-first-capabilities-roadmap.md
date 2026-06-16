# Task 048 Phase 5B Text-first Capabilities Roadmap

## Goal

确认 Phase 5B 只聚焦 direct_chat 和 image_generation 两个 text-first 能力，不扩展其他真实 Provider。

## Read first

- `docs/48-phase5b-text-first-capabilities-roadmap.md`
- `docs/43-direct-chat-and-text-only-capabilities.md`
- `docs/47-phase5a-assistant-routing-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5B 目标为 text-first capabilities。
- 明确 direct_chat 和 image_generation 不依赖图片/视频。
- 明确默认仍使用 MockAdapter。
- 明确不接入商品搜索、渲染、Vision hardening。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/48-phase5b-text-first-capabilities-roadmap.md
tasks/README_PHASE5B.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 049。
