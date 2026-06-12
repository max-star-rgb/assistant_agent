# 31 失败恢复策略设计

## 背景

真实 Provider、长任务和多步 Agent 都会失败。Phase 4 需要建立统一失败恢复策略。

## 失败类型

```text
intent_unclear
missing_required_input
tool_not_found
tool_input_invalid
provider_unconfigured
provider_timeout
provider_rate_limited
provider_bad_response
memory_unavailable
task_cancelled
unknown_error
```

## 恢复动作

```text
ask_followup
skip_step
retry_step
fallback_to_mock
fallback_to_text_response
stop_with_error
continue_with_partial_result
```

## RecoveryPolicy

推荐：

```python
class RecoveryPolicy(BaseModel):
    max_retries: int = 1
    allow_skip_optional_steps: bool = True
    allow_partial_response: bool = True
    fallback_to_mock: bool = False
```

## 多步任务中的失败处理

示例：

```text
搜索成功
比价失败
图片生成仍可继续
最终响应说明比价失败原因
```

不应因为一个非关键步骤失败就让整个 Agent 崩溃。

## 用户确认

以下场景应进入 followup / confirmation：

- 意图不明确。
- 缺少关键输入。
- 工具执行有风险。
- 结果不确定但会影响后续任务。

## 验收标准

- Provider timeout 可被结构化记录。
- 可选步骤失败后允许继续。
- 关键步骤失败后返回结构化错误。
- final response 能说明部分成功/失败。
