---
name: memory_recall
description: Retrieve stored user preferences, prior facts, or earlier context through the governed memory_retrieval tool.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- memory_retrieval

## Permissions
- tool:memory_retrieval

## Required Inputs
- memory_retrieval: query

## When To Use
- User asks about previous conversations, saved preferences, remembered facts, or prior tasks.
- User says "上次", "之前", "我保存过", "按我的偏好", "记忆里", or asks to continue from stored context.
- User needs durable personal context that may have been stored before this run.

## When Not To Use
- User asks for latest, current, today, news, or web-backed information; use web_search instead.
- User asks a first-time general question without referencing saved context.
- User asks to save new information; use memory_capture instead.

## Safe Examples
- what did I say my preferred language was
- continue from my saved project preferences
- 上次我让你记住的配置是什么
- 按我的已保存偏好推荐

## Runtime Constraints
- Read-only memory lookup; do not write memory from this skill.
- Memory is not a source of current facts, prices, news, or provider state.
- Execute only through MemoryManager and ToolExecutor boundaries.
- Do not reveal raw memory store internals or audit metadata.
