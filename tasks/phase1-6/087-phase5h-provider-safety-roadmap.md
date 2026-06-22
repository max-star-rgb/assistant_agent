# Task 087 Phase 5H Provider Safety Roadmap

## Goal

确认 Phase 5H 只做 Provider Safety / Retry / Cost / Trace Query，不新增真实 Provider。

## Read first

- `docs/91-phase5h-provider-safety-roadmap.md`
- `docs/90-phase5g-video-understanding-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5H 是横向 Provider Safety 阶段。
- 明确不新增真实 Provider。
- 明确不默认调用真实 Provider。
- 明确覆盖 error mapping、timeout、retry、fallback、budget、redaction、trace query。
- 明确默认 mock/local-first。
- 不写入 API Key。

## Suggested files

```text
docs/91-phase5h-provider-safety-roadmap.md
tasks/README_PHASE5H.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 088。
