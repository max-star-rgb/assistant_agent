# Task 041 Assistant Capability Routing Roadmap

## Goal

替换 Vision-only Phase 5A 主线，确认项目定位为 Intent-driven Assistant Agent。

## Read first

- `docs/41-phase5a-assistant-capability-routing-roadmap.md`
- `docs/42-assistant-capability-routing-baseline.md`
- `docs/45-vision-provider-validation-note.md`
- `docs/01-architecture.md`
- `docs/04-intent-and-routing.md`

## Scope

只做文档与任务主线修正，不做业务代码大改。

## Requirements

- 更新 Phase 5A 文档主线为 Assistant Capability Routing。
- 明确 direct_chat 和 image_generation 支持纯文本输入。
- 明确真实 Qwen Vision smoke 是 Provider validation，不是 Phase 5A 主线。
- 明确 Vision-only 旧任务应降级或暂缓。
- 不删除已有 Vision 文档，避免丢历史记录。
- 不调用真实 Provider。
- 不写入 API Key。

## Suggested files

```text
docs/41-phase5a-assistant-capability-routing-roadmap.md
docs/45-vision-provider-validation-note.md
tasks/README_PHASE5A.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 042。
