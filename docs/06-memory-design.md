# 06 记忆系统设计

## 1. 为什么需要记忆

用户会使用上下文表达：

- “上次那个黑色包还在吗？”
- “刚才视频里的第二双鞋”
- “我喜欢之前那个日系风格”

这些都需要 Agent 检索历史视频、商品、偏好和任务状态。

## 2. 记忆类型

| 类型 | 内容 |
|---|---|
| SessionMemory | 当前会话多轮状态 |
| VideoMemory | 视频片段、关键帧、视觉摘要 |
| ProductMemory | 看过/比较过/收藏过的商品 |
| PreferenceMemory | 用户喜欢的颜色、风格、价格、品牌 |
| TaskMemory | 未完成任务、追问状态、工具调用结果 |

## 3. MVP 实现

开发阶段使用内存 + JSON 文件持久化：

```text
.data/
├── memories.jsonl
├── sessions.json
└── tool_calls.jsonl
```

后续替换：

- PostgreSQL：结构化记忆
- 向量库：语义检索
- 对象存储：视频、图片、渲染结果

## 4. MemoryItem

```python
class MemoryItem(BaseModel):
    memory_id: str
    user_id: str
    session_id: str | None = None
    memory_type: Literal["session", "video", "product", "preference", "task"]
    content: str
    metadata: dict = {}
    tags: list[str] = []
    created_at: datetime
```

## 5. 检索策略

MVP：关键词 + tags + 最近时间排序。

后续：embedding 相似度 + 时间衰减 + 用户偏好权重。

## 6. 写入策略

以下内容应写入记忆：

- 用户明确要求“记住”。
- 视频理解摘要。
- 用户确认喜欢/不喜欢的商品、风格、颜色。
- 多工具任务的最终结果。
- 未完成任务状态。

## 7. 隐私边界

- 不保存敏感信息，除非用户明确要求且系统允许。
- 记忆必须按 user_id 隔离。
- 用户应能删除自己的记忆。
