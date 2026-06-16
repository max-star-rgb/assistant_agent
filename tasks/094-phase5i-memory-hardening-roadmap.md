# Task 094 Phase 5I Memory Hardening Roadmap

## Goal

确认 Phase 5I 聚焦 Memory Hardening，不做 MCP / Skills，不接外部 memory service。

## Read first

- `docs/99-phase5i-memory-hardening-roadmap.md`
- `docs/98-phase5h-provider-safety-review.md`
- 当前 README / docs index

## Scope

只更新文档和阶段说明，不做业务代码大改。

## Requirements

- 明确 Phase 5I 目标为 Memory Hardening。
- 明确默认 local-first。
- 明确不接 Vector DB。
- 明确不做复杂 RAG 平台。
- 明确不做 MCP / Skills。
- 明确 user isolation / privacy / write policy 是重点。
- 不调用外部服务。
- 不写入 API Key。

## Suggested files

```text
docs/99-phase5i-memory-hardening-roadmap.md
tasks/README_PHASE5I.md
README.md
```

## Acceptance

```bash
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 095。
