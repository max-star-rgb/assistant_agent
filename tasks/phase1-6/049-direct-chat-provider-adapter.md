# Task 049 Direct Chat Provider Adapter

## Goal

为 direct_chat 增加 adapter contract，使聊天能力可从 mock/local 切换到可选真实 Provider。

## Read first

- `docs/49-direct-chat-provider-design.md`
- 当前 intent/router/runtime
- 当前 tools/services adapter 结构
- 当前 response composer

## Requirements

- 定义 ChatAdapter Protocol 或等价接口。
- 定义 ChatRequest / ChatResult schema。
- 新增 MockChatAdapter 或 LocalChatAdapter。
- direct_chat 路径通过 adapter 返回 response。
- 不默认调用真实 LLM Provider。
- 可预留 provider config，但无 key 时不失败。
- Tool/Runtime 不直接调用 Provider SDK。

## Tests

新增或更新：

```text
tests/test_direct_chat_adapter.py
tests/test_direct_chat_routing.py
```

覆盖：

- 纯文本 direct_chat。
- 不触发 image/video understanding。
- mock chat result。
- provider_unconfigured 路径如有真实 skeleton。

## Acceptance

```bash
python -m pytest
python scripts/run_evals.py
```

## Stop condition

完成后停止，不要继续 Task 050。
