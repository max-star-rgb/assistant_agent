# 103 Memory Privacy and User Isolation

## 目标

确保 memory 不跨用户泄漏，不保存敏感信息，并且查询、写入、删除都受到 user/session 边界保护。

## 用户隔离

所有 memory 操作必须带：

```text
user_id
```

如果没有 user_id，至少应使用安全默认：

```text
anonymous/local/single-user
```

但多用户模式下不得跨 user_id 检索。

## Session 边界

session_id 用于短期上下文过滤：

```text
当前会话优先
同用户历史其次
```

## 敏感信息脱敏

写入 memory 前应过滤：

```text
API Key
Authorization header
Bearer token
cookie
password
secret
完整 base64
本地隐私绝对路径
身份证号/电话/邮箱等可选 PII patterns
```

Phase 5I 可以先复用 Phase 5H 的 redaction policy。

当前实现复用 `sanitize_error_message()`：

- `MemoryItem.summary`、`reason`、`content` 字符串值、`tags` 会做脱敏。
- 危险 key 仍直接拒绝写入，例如 `api_key`、`authorization`、`bearer`、`cookie`、`password`、`secret`、`token`、`base64`、`raw_*`、`provider_response`。
- 内联 `data:image/...`、`data:video/...`、`data:audio/...` 会被拒绝。

## Sensitivity

MemoryItem 建议包含：

```text
sensitivity = normal | sensitive | private
```

默认：

```text
normal
```

如果检测到敏感内容：

```text
require explicit save
or skip save
```

当前策略：

- 显式保存中如果文本被脱敏，`MemoryItem.sensitivity` 标记为 `sensitive`。
- 自动 task summary 如果包含可脱敏敏感内容，默认不保存。

## 查询权限

MemoryStore 方法应接收 user_id，并且只返回同 user_id 的结果。

错误示例：

```text
search(query="上次那个包")
  ↓
返回其他用户的商品
```

正确做法：

```text
search(user_id="u1", query="上次那个包")
  ↓
只返回 u1 的记忆
```

## Logging

日志、trace、eval 输出不应包含完整 memory content，最多输出：

```text
memory_id
memory_type
summary preview
tags
count
```

当前 trace state summary 只输出状态、intent、plan/tool/result/error 计数和 step index，不展开 `memory_context` 或 memory `content`。

## 验收标准

- memory search 按 user_id 过滤。
- memory delete 按 user_id 校验。
- memory context 不包含 secret。
- sensitive memory 默认不自动保存。
- trace/log 不输出完整敏感 memory。
- 默认测试离线。
