# Task 109 Assistant CLI / Local Demo Entry

## Goal

新增或完善本地 CLI，让用户可以通过命令行运行 Assistant Agent。

## Read first

- `docs/116-phase6a-local-demo-entry-roadmap.md`
- 当前 scripts/
- 当前 AgentGraphRuntime
- 当前 demo scenarios

## Requirements

- 新增或完善 `scripts/run_assistant_cli.py`。
- 支持 `--text`。
- 支持 `--scenario`。
- 支持可选 `--image-ref` / `--video-ref`。
- 输出 response_text、tool_sequence、run_id、trace_id、errors。
- 默认 mock/local。
- 不调用真实 Provider。

## Acceptance

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
python -m pytest
```
