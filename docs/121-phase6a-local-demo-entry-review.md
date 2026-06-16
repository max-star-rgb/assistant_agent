# 121 Phase 6A Local Demo Entry Review

## Conclusion

Phase 6A Local Demo Entry / CLI is complete. The project now has a local CLI entry, a stable offline demo runner, and local user-facing runbooks for running the assistant without reading internal task documents.

## 1. CLI Status

The local assistant CLI is:

```text
scripts/run_assistant_cli.py
```

It supports:

- `--text`
- `--scenario`
- optional `--image-ref`
- optional `--video-ref`
- JSON output
- readable text output through `--format text`

Default CLI runs use:

```text
AgentGraphRuntime(config=ProviderConfig())
```

This keeps CLI execution on mock/local defaults.

CLI output includes:

- `response_text`
- `tool_sequence`
- `run_id`
- `trace_id`
- `errors`
- `offline`

## 2. Demo Scenarios Status

The offline scenario matrix is:

```text
demo_data/scenarios/e2e_demo_scenarios.json
```

Current status:

- 17 scenarios are available.
- All scenarios run through `scripts/run_demo_flows.py`.
- Scenario ids are stable and human-readable.
- Expected tool sequences are checked by the demo runner.
- Response quality checks reject generic `"已完成请求处理。"` responses.
- Media scenarios use mock metadata ids instead of real media files.

## 3. Default Mock / Local Boundary

Phase 6A does not add new capabilities and does not call real Providers by default.

Default commands remain offline:

```bash
python scripts/run_assistant_cli.py --text "帮我写一段商品介绍"
python scripts/run_assistant_cli.py --scenario product_search_compare
python scripts/run_demo_flows.py
python -m pytest
```

No API key is required for Phase 6A local demo commands.

## 4. Remaining Issues

- The CLI is intentionally minimal and local-only.
- The CLI does not provide an interactive REPL yet.
- Scenario listing is documented through the scenario JSON file, not exposed as a dedicated CLI command.
- Real Provider demos remain out of scope for Phase 6A.
- No web UI is included in Phase 6A.

## 5. Phase 6B Recommendation

Proceed to Phase 6B: FastAPI Demo & Simple Web Console.

Recommended next work:

- Stabilize FastAPI demo contracts for listing scenarios and running agent demos.
- Add a simple web console for entering text and selecting demo scenarios.
- Display response text, tool sequence, run id, trace id, and errors in the web console.
- Preserve the same mock/local default boundary.
