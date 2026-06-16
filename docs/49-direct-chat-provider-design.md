# 49 Direct Chat Provider Design

## 目标

让 direct_chat 能力从 routing-only 变成可替换 Provider 的能力模块。

## 能力定义

direct_chat 用于：

```text
普通聊天
文案生成
概念解释
建议
任务澄清
不需要外部工具的文本回答
```

## 推荐链路

```text
AgentGraphRuntime
  ↓
direct_chat capability
  ↓
ChatTool or ResponseComposer
  ↓
ChatAdapter
  ↓
MockChatAdapter / RealChatProviderAdapter
```

## Adapter 接口建议

```python
class ChatAdapter(Protocol):
    def chat(self, request: ChatRequest) -> ChatResult:
        ...
```

## ChatRequest

建议字段：

```text
user_id
session_id
user_query
memory_context
system_instruction
temperature
max_tokens
```

## ChatResult

建议字段：

```text
response_text
provider
model
usage optional
latency_ms optional
errors
output_ref optional
```

## 默认实现

默认必须是：

```text
MockChatAdapter / LocalChatAdapter
```

默认测试不调用真实 LLM Provider。

## 真实 Provider

真实 Provider 可后续接入：

```text
OpenAI-compatible chat endpoint
Qwen chat endpoint
Local LLM service
```

Phase 5B 可以先做 skeleton 和配置，不强制真实调用。

## 错误处理

统一错误码：

```text
provider_unconfigured
provider_timeout
provider_bad_response
provider_auth_failed
provider_rate_limited
```

## 验收标准

- direct_chat 不需要图片或视频。
- direct_chat 不触发 image_understanding。
- direct_chat 可通过 adapter 替换 provider。
- 默认 pytest 离线运行。
