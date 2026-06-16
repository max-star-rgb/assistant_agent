# 94 Provider Call Budget and Cost Guard

## 目标

为 Agent run 增加调用预算和成本保护，避免真实 Provider 接入后出现失控调用。

## 为什么需要预算

一个多步任务可能触发多个 Provider：

```text
video_understanding
product_search
price_compare
image_generation
render_3d
response_composer
```

如果没有限制，错误的 planner 或循环可能导致：

```text
重复调用
超额成本
超时
限流
不必要的真实 Provider 调用
```

## ProviderCallBudget

建议新增：

```python
class ProviderCallBudget(BaseModel):
    max_provider_calls_per_run: int = 10
    max_calls_per_capability: dict[str, int] = {}
    max_estimated_cost_per_run: float | None = None
    max_input_bytes_per_run: int | None = None
    allow_real_provider: bool = False
```

## 计数维度

至少记录：

```text
run_id
capability
provider
model
call_count
estimated_cost
input_size_bytes
latency_ms
status
```

## 默认策略

默认：

```text
allow_real_provider = False
max_provider_calls_per_run = conservative
```

真实 Provider 只在显式配置下启用。

## Cost Estimate

Phase 5H 不需要接真实计费系统，可以先做估算字段：

```text
estimated_cost
cost_unit
unknown
```

如果无法估算：

```text
estimated_cost = null
```

但仍记录调用次数。

## Budget Exceeded

错误码：

```text
provider_budget_exceeded
provider_call_limit_exceeded
provider_input_size_exceeded
```

## 与 LangGraph Loop 的关系

Graph loop 每次执行 Provider 前应检查 budget。

如果预算不足：

```text
停止该 step
记录结构化错误
进入 partial response
```

## 验收标准

- 每个 run 有 provider call counter。
- 超过 max_provider_calls_per_run 时停止调用。
- 每个 capability 可配置 max calls。
- 真实 Provider 默认不启用。
- 预算错误结构化。
- 默认测试离线。
