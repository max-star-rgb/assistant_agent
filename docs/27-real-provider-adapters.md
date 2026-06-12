# 27 真实 Provider Adapter 接入设计

## 目标

当前项目已有 Provider 配置和 MockAdapter 契约测试。Phase 4 开始增加真实 Provider Adapter 的可选实现，但不能破坏离线测试与 Mock 默认行为。

## 设计原则

Tool 仍然只依赖 Adapter Protocol。

```text
Tool
  ↓
Adapter Protocol
  ↓
MockAdapter / RealProviderAdapter
  ↓
External Provider
```

禁止：

```text
Tool
  ↓
Provider SDK / HTTP API
```

## 推荐接入顺序

优先接一个最容易验证的真实 Provider，而不是一次接满全部能力。

推荐顺序：

1. Vision Provider
2. Image Generation Provider
3. Product Search Provider
4. Render Provider

## Provider 命名建议

```text
src/multimodal_agent/services/
├── vision_adapter.py
├── openai_vision_adapter.py
├── qwen_vision_adapter.py
├── image_generation_adapter.py
├── openai_image_adapter.py
├── comfyui_image_adapter.py
├── product_adapter.py
├── http_product_search_adapter.py
├── render_adapter.py
└── http_render_adapter.py
```

实际文件名应匹配当前项目已有命名，不强制照搬。

## 配置方式

通过环境变量读取：

```text
MULTIMODAL_AGENT_VISION_PROVIDER=mock|openai|qwen
MULTIMODAL_AGENT_IMAGE_PROVIDER=mock|openai|comfyui
MULTIMODAL_AGENT_PRODUCT_PROVIDER=mock|http
MULTIMODAL_AGENT_RENDER_PROVIDER=mock|http

OPENAI_API_KEY=
QWEN_API_KEY=
COMFYUI_BASE_URL=
PRODUCT_SEARCH_BASE_URL=
RENDER_BASE_URL=
```

默认必须是 `mock`。

## 错误处理要求

真实 Adapter 不应抛出未捕获异常到 Tool 层。

应转换为结构化错误：

```text
provider_unconfigured
provider_timeout
provider_auth_failed
provider_bad_response
provider_rate_limited
provider_unavailable
```

## 测试要求

- Unit tests 默认使用 MockAdapter。
- Contract tests 默认运行。
- Integration tests 默认 skip。
- 只有设置 `RUN_INTEGRATION_TESTS=1` 且配置齐全时才调用真实 Provider。

## 验收标准

- 至少一个真实 Provider Adapter 可选实现。
- 无配置时不影响测试。
- Tool 层无需修改即可切换 Provider。
- 代码中无硬编码密钥。
