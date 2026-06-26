# Tests Review

This is a read-only review of `tests/**`. No test files were edited for this task.

## 当前 tests 目录概况

- Python test files: 193 `test_*.py` files.
- Top-level tests: 166 files, covering most runtime behavior directly.
- `tests/unit/`: 17 focused unit tests.
- `tests/contracts/`: 4 adapter/tool contract tests.
- `tests/e2e/`: 1 e2e demo-flow test file.
- `tests/integration/`: 5 integration test files, skipped by default unless `RUN_INTEGRATION_TESTS=1`.
- `tests/evals/eval_cases.json`: offline eval case matrix used by `scripts/run_evals.py`.
- `tests/node/haodanku_order_query.test.mjs`: Node-side Haodanku order-query test.
- Generated cache directories such as `tests/**/__pycache__` are present; they should be cleaned only in a later approved cleanup task.

`tests/conftest.py` enforces offline defaults by disabling dotenv loading and clearing provider-related environment variables unless integration tests are explicitly enabled.

## 测试类型

| type | examples | current role |
| --- | --- | --- |
| unit | `tests/unit/test_provider_config.py`, `tests/unit/test_tool_registry.py` | Core schema/config/tool behavior |
| contract | `tests/contracts/test_*_adapter_contract.py` | Adapter/tool result contract stability |
| integration | `tests/integration/test_real_provider_adapters.py`, `tests/integration/test_websocket_events.py` | Explicit opt-in provider/API integration checks |
| e2e | `tests/e2e/test_demo_flow.py`, `tests/test_e2e_demo_runner.py` | Offline end-to-end demo validation |
| eval | `tests/evals/eval_cases.json`, `tests/test_*_evals.py` | Routing/capability/provider safety regression cases |
| demo/script smoke | `tests/test_*_smoke_script.py`, `tests/test_run_server.py`, `tests/test_run_client.py` | Script import, offline command, and safety behavior |

## 当前架构仍需要的测试

- ReAct/assistant-loop tests: `test_assistant_loop_graph.py`, `test_phase8a1_react_action_quality.py`, `test_phase8a2_react_final_answer_handoff.py`, `test_native_tool_call_handoff.py`, `test_plan_mode_react.py`.
- Provider safety/config tests: `test_runtime_profile.py`, `test_runtime_profile_safety.py`, `test_provider_config_validation.py`, `test_provider_readiness.py`, `test_provider_selection.py`, `unit/test_provider_config.py`.
- Tool and adapter tests: product search, price compare, vision, video, image generation, render, Haodanku, and contract tests.
- API/WebSocket tests: `test_api_*`, `test_websocket_*`, `test_session_api.py`, `test_trace_query_api.py`.
- Memory tests: `test_memory_*`, `test_explicit_memory_e2e.py`, `test_memory_capability_contract_integration.py`.
- Safety/redaction tests: `test_sensitive_redaction.py`, `test_trace_redaction.py`, `test_fallback_policy.py`, `test_provider_safety_*`.
- Demo/eval tests: `test_demo_scenario_matrix.py`, `test_e2e_demo_runner.py`, `test_eval_suite_layering.py`, `test_*_evals.py`.

## 可能是历史遗留的测试

- Old conditional graph / intent-router compatibility tests may still be useful, but need product decision before cleanup.
- Planner and multistep tests that predate Phase 8 may overlap with current plan-mode/ReAct behavior.
- Phase 6/7 productization tests may be historical acceptance checks, but some still guard Web Console/API behavior.
- Top-level `tests/test_*.py` contains many categories mixed together; the placement is historical even when the tests remain valuable.
- `tests/**/__pycache__` is generated output, not source tests.

## 哪些测试不要动

- Do not modify any `tests/**` file in this documentation-only task.
- Do not modify integration gating unless a dedicated provider/integration task asks for it.
- Do not remove compatibility tests without first documenting which runtime behavior is no longer supported.
- Do not change tests to make real providers run by default.
- Do not rewrite tests to hide current failures during a docs audit.

## 建议后续如何整理

1. Run the full offline suite and record current failures before any cleanup.
2. Remove generated caches (`__pycache__`, `.pytest_cache`) only in a separate cleanup task.
3. Add pytest markers or directory grouping for `unit`, `contracts`, `e2e`, `integration`, `eval`, and `script_smoke`.
4. Decide whether old `conditional`/intent-router behavior is still supported. If supported, keep compatibility tests; if not, archive the tests with a migration note.
5. Move historical Phase 6/7 acceptance tests only after confirming the behavior is covered by current API/Web/ReAct tests.
6. Keep integration tests opt-in with `RUN_INTEGRATION_TESTS=1` and explicit provider configuration.
