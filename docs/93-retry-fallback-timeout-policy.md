# 93 Retry / Fallback / Timeout Policy

## 目标

为 Provider 调用建立统一的超时、重试和降级策略，避免真实 Provider 接入后出现无限等待、无限重试或不清晰失败。

## Timeout Policy

建议配置：

```text
MULTIMODAL_AGENT_DEFAULT_PROVIDER_TIMEOUT_SECONDS=30
MULTIMODAL_AGENT_CHAT_TIMEOUT_SECONDS=30
MULTIMODAL_AGENT_IMAGE_TIMEOUT_SECONDS=60
MULTIMODAL_AGENT_VISION_TIMEOUT_SECONDS=60
MULTIMODAL_AGENT_VIDEO_TIMEOUT_SECONDS=120
MULTIMODAL_AGENT_SEARCH_TIMEOUT_SECONDS=20
MULTIMODAL_AGENT_RENDER_TIMEOUT_SECONDS=120
```

默认值应保守。

## Retry Policy

建议新增：

```python
class RetryPolicy(BaseModel):
    max_retries: int = 1
    backoff_seconds: float = 0.5
    retry_on: list[str] = ["provider_timeout", "provider_network_error", "provider_rate_limited"]
```

## 重试原则

可以重试：

```text
provider_timeout
provider_network_error
provider_rate_limited
provider_unavailable
```

不应重试：

```text
provider_auth_failed
provider_unconfigured
provider_request_invalid
provider_request_too_large
provider_unsupported_format
```

## Fallback Policy

建议新增：

```python
class FallbackPolicy(BaseModel):
    allow_mock_fallback: bool = False
    allow_partial_result: bool = True
    fallback_providers: dict[str, list[str]] = {}
```

## 重要边界

默认不应把真实 Provider 失败静默 fallback 成 Mock 成功。

错误示例：

```text
真实 qwen 失败
  ↓
静默返回 mock://...
```

正确做法：

```text
真实 qwen 失败
  ↓
返回 provider_timeout 或 provider_bad_response
  ↓
如果允许 partial result，response composer 说明部分失败
```

Mock fallback 只有在显式配置时才允许：

```text
MULTIMODAL_AGENT_ALLOW_MOCK_FALLBACK=1
```

## Partial Result

多步任务中，非关键步骤失败时可继续。

示例：

```text
product_search succeeded
price_compare failed
image_generation succeeded
```

最终响应应说明：

```text
已完成商品搜索和图片生成，但比价失败，原因是价格 Provider 超时。
```

## 验收标准

- timeout policy 可配置。
- retry policy 可配置。
- 不可重试错误不会重试。
- provider_unconfigured 不重试。
- mock fallback 默认关闭。
- partial result 可被 response composer 总结。
- 默认测试离线。
