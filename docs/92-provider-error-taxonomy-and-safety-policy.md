# 92 Provider Error Taxonomy and Safety Policy

## 目标

统一所有 Provider 的错误码和错误结构，避免不同 Adapter 返回不一致的异常格式。

## 当前问题

不同 Provider 可能出现：

```text
缺 API Key
认证失败
超时
限流
响应格式异常
网络不可达
请求过大
Provider 服务不可用
内部异常
```

如果每个 Adapter 自己随意返回错误，Response Composer、API、WebSocket、Trace、Eval 都会变得难以维护。

## 统一错误结构

建议统一为：

```python
class ProviderError(BaseModel):
    code: str
    message: str
    detail: dict = {}
    recoverable: bool = False
    provider: str | None = None
    capability: str | None = None
```

## 推荐错误码

### 配置类

```text
provider_unconfigured
provider_missing_api_key
provider_missing_base_url
provider_invalid_config
```

### 请求类

```text
provider_request_invalid
provider_request_too_large
provider_unsupported_input
provider_unsupported_format
```

### 网络和服务类

```text
provider_timeout
provider_network_error
provider_unavailable
provider_bad_gateway
```

### 认证和限流类

```text
provider_auth_failed
provider_permission_denied
provider_rate_limited
```

### 响应类

```text
provider_bad_response
provider_empty_response
provider_schema_mismatch
```

### 执行类

```text
provider_execution_failed
provider_cancelled
provider_unknown_error
```

## Error Mapping 要求

所有 Adapter 应把内部异常映射为稳定错误码。

禁止：

```text
直接把 traceback 返回给用户
直接返回 provider raw error
直接暴露 Authorization header
直接暴露 API Key
直接暴露完整请求体
```

## SafetyPolicy

建议新增：

```text
src/multimodal_agent/services/provider_safety.py
```

包含：

```text
ProviderSafetyPolicy
ProviderError
sanitize_error_message()
map_exception_to_provider_error()
```

## 脱敏规则

必须脱敏：

```text
API Key
Authorization
Bearer token
cookie
secret
password
完整 base64
本地隐私绝对路径
超长 provider raw response
```

## 验收标准

- ProviderError schema 存在。
- 常见错误码统一。
- 各 Provider Adapter 不直接泄露 raw exception。
- 错误消息经过脱敏。
- API / WebSocket / Trace 统一使用 ProviderError 或兼容结构。
- 默认测试离线。
