# 86 Video Provider Adapter and Safety

## 目标

为外部 Video MLLM Provider 建立可选接入结构和安全边界，但默认不调用真实外部服务。

## Provider 类型

### mock

默认 provider。

```text
MULTIMODAL_AGENT_VIDEO_PROVIDER=mock
```

### http

通用 HTTP Video MLLM 服务。

```text
MULTIMODAL_AGENT_VIDEO_PROVIDER=http
VIDEO_UNDERSTANDING_BASE_URL=
VIDEO_UNDERSTANDING_API_KEY=
VIDEO_UNDERSTANDING_MODEL=
```

### qwen / openai_compatible

可作为后续扩展，但 Phase 5G 不强制真实调用。

## 直接视频输入与抽帧 fallback

### 直接视频 Provider

如果 Provider 支持视频输入：

```text
video_ref
  ↓
Provider
```

### 图片 VLM fallback

如果 Provider 只支持图片输入，Adapter 可在内部执行轻量抽帧：

```text
video_ref
  ↓
Adapter internal frame sampling
  ↓
Image VLM Provider
  ↓
merge result
```

注意：

- 抽帧是 Adapter 内部实现。
- Agent 不处理帧。
- Phase 5G 不实现复杂抽帧系统。
- 默认 mock 不抽帧。

## 请求大小保护

建议配置：

```text
MULTIMODAL_AGENT_MAX_VIDEO_BYTES=52428800
MULTIMODAL_AGENT_MAX_VIDEO_SECONDS=60
```

默认限制可以保守，防止误上传大视频。

## 超时保护

建议配置：

```text
VIDEO_UNDERSTANDING_TIMEOUT_SECONDS=60
```

缺省时使用安全默认值。

## 隐私边界

视频通常比图片更敏感。必须明确：

- 不默认调用真实 Provider。
- 不自动上传用户视频。
- 不提交真实视频文件。
- 不在 trace 中记录完整视频内容。
- 不在日志中记录本地隐私路径。
- smoke 中只输出文件名或安全引用，不输出完整敏感路径。
- 真实视频 smoke 由用户本地手动执行。

## Error Mapping

真实 Provider 错误应映射为统一错误：

```text
provider_unconfigured
provider_timeout
provider_bad_response
provider_auth_failed
provider_rate_limited
video_file_too_large
video_unsupported_format
video_provider_unavailable
```

## Integration Tests

真实 Provider 测试必须 env-gated：

```bash
RUN_INTEGRATION_TESTS=1 python -m pytest tests/integration
```

默认 pytest 必须离线。

## 验收标准

- default provider 为 mock。
- http provider 缺配置时返回 provider_unconfigured。
- 大文件限制有结构化错误。
- 超时可配置。
- 默认测试不调用真实 Provider。
- 不泄露 API Key / Bearer token / Authorization header。
