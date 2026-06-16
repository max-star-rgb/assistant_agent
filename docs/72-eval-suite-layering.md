# 72 Eval Suite Layering

## 目标

将当前 eval / tests 按职责分层，避免 routing、tool contract、API、smoke、E2E demo 混在一起。

## 推荐分层

```text
routing eval
tool contract eval
api contract eval
smoke eval
e2e demo eval
```

## Routing Eval

检查：

```text
intent
capability
expected_tools
tool order
unexpected tools
followup
```

默认文件：

```text
tests/evals/eval_cases.json
```

## Tool Contract Eval

检查每个 Tool 输出是否满足统一 contract。

覆盖：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
```

## API Contract Eval

检查 HTTP / WebSocket 输出稳定：

```text
protocol_version
run_id
trace_id
status
intent
tool_calls
tool_results
contract
errors
```

## Smoke Eval

Smoke 只用于手动或显式运行：

```text
scripts/smoke_*.py
```

默认 pytest 不触发真实 Provider。

## E2E Demo Eval

检查完整 demo flow 是否可跑通：

```text
scenario_id
user_query
inputs
expected_tool_sequence
expected_response_contains
```

## Runner 建议

可以保留：

```text
scripts/run_evals.py
```

并增加 mode：

```bash
python scripts/run_evals.py --suite routing
python scripts/run_evals.py --suite e2e
python scripts/run_evals.py --suite all
```

默认：

```text
routing + e2e mock-only
```

## 指标建议

```text
routing_pass_rate
tool_contract_pass_rate
api_contract_pass_rate
e2e_pass_rate
unexpected_tool_rate
response_quality_pass_rate
```

## 验收标准

- eval cases 有 suite/category 字段。
- run_evals 可按 suite 运行。
- 默认不调用真实 Provider。
- E2E demo cases 可复现。
