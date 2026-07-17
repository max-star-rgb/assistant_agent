---
name: memory_capture
description: Save durable user preferences or reusable future facts through the governed memory_save tool when the user asks to remember them.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- memory_save

## Permissions
- tool:memory_save

## Required Inputs
- memory_save: content, source_intent, source_reason, future_use, evidence

## When To Use
- User explicitly asks to remember, save, store, or keep a preference or durable fact for future use.
- User confirms that a stable preference, project fact, or reusable instruction should be saved.
- 用户说“记住”“帮我保存”“以后都按这个”“把这个偏好存下来”。

## When Not To Use
- User asks for one-off output, temporary search results, transient calculations, or draft text.
- User provides sensitive personal information that should not be stored.
- User asks to recall existing memory; use memory_recall instead.

## Safe Examples
- remember that I prefer Chinese replies
- save this project preference for later
- 记住我默认用 hello_agent 环境
- 以后生成代码时按这个风格

## Runtime Constraints
- Memory writes remain confirmation- and policy-sensitive; this descriptor does not bypass MemoryManager gates.
- Provide source_intent, source_reason, future_use, and evidence for assistant-loop memory_save calls.
- Do not save secrets, credentials, raw provider responses, temporary search results, or sensitive data.
- Execute only through ToolExecutor and the configured memory policy.
