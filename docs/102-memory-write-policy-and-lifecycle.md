# 102 Memory Write Policy and Lifecycle

## 目标

定义什么时候写入记忆、写什么、保留多久、如何删除，避免 Agent 把所有内容都永久保存。

## 为什么需要写入策略

不加控制的 memory_save 会导致：

```text
保存过多无关内容
保存敏感信息
记忆污染
检索噪声增加
用户难以删除
跨任务误用
```

## Memory Write Policy

建议新增：

```python
class MemoryWritePolicy(BaseModel):
    auto_save_preferences: bool = True
    auto_save_artifacts: bool = True
    auto_save_task_summary: bool = True
    auto_save_raw_user_text: bool = False
    auto_save_media_raw: bool = False
    require_explicit_save_for_sensitive: bool = True
```

## 自动保存什么

可以自动保存：

```text
用户明确偏好
任务摘要
商品搜索摘要
比价结果摘要
生成图片 output_ref
渲染 output_ref
图片/视频理解摘要
```

不应自动保存：

```text
API Key
Authorization
身份证/合同/票据内容
完整媒体 base64
完整 provider raw response
大文件内容
敏感原文
```

## 显式保存

用户说：

```text
记住这个风格
记住这个商品
以后都按这个预算
```

应写入 preference_memory / product_memory。

## 生命周期

建议字段：

```text
created_at
updated_at
expires_at
deleted_at optional
```

### 默认过期策略

可以按类型设置：

```text
conversation: 短期
task: 中期
preference: 长期
artifact: 中期
product: 中期
video/image summary: 中期
```

Phase 5I 不需要复杂 TTL 系统，但应有字段和基础清理函数。

## 删除

应支持：

```text
delete(memory_id, user_id)
delete_by_session(session_id, user_id)
```

至少要保证不会跨 user 删除。

## 保存时机

建议在 LangGraph 中：

```text
save_memory_node
```

或在 runtime 结束后统一写入摘要。

不要在每个 tool 内部随意写 memory。

## 验收标准

- MemoryWritePolicy 存在。
- 默认不保存 raw media / API Key / raw provider response。
- 显式“记住”请求会写入 memory。
- task summary 可写入。
- artifact output_ref 可写入。
- 支持 delete by user。
- 默认本地离线。
