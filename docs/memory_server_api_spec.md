# Mem0 HTTP 接入契约

最后更新：2026-07-24

项目不再定义独立的 Memory Server 协议。本文件只记录 `assistant_agent` 实际使用的
Mem0 OSS REST 子集；完整行为以 Mem0 官方 API 为准。

## 身份

召回请求携带项目从可信 runtime 身份生成的不透明 `user_id` 和 `agent_id`；写入请求还必须
携带由 `user_id + agent_id + session_id` 稳定生成的不透明 `run_id`。用户输入不能直接覆盖这些字段。

## Session 启动召回

```http
GET /memories?user_id=<opaque>&agent_id=<opaque>&limit=5
```

响应只消费 `results` 数组中每条记录的 `id`、`memory`、`created_at` 和可选 `score`。
该调用只发生在 session 创建阶段。

## Turn capture

```http
POST /memories
Content-Type: application/json

{
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "user_id": "<opaque>",
  "agent_id": "<opaque>",
  "run_id": "<opaque>",
  "metadata": {
    "source": "runtime_turn_capture",
    "source_turn": "<opaque>",
    "occurred_at": "<ISO-8601>"
  }
}
```

项目不设置 `infer=false`，不发送自定义 extraction prompt，不创建 core/daily 双记录。
该后台请求由 adapter 自动使用至少 30 秒超时；不复用 session-start recall 的 5 秒前台超时。

响应只消费原生 `results` 中每条合法记录的 `id`、`memory` 和 `event`。支持的 event 为
`ADD`、`UPDATE`、`DELETE`；空数组表示没有提炼出长期记忆。memory text 只可进入显式启用的
本机观测 overlay，不进入 canonical trace 或普通日志。

## 错误语义

- recall 失败：session snapshot 为空并标记 `mem0_recall_failed`，session/turn 继续。
- capture 失败：后台任务记录 `mem0_capture_failed`，已返回的 assistant 回复不受影响。
- 响应原文、URL、凭据和异常细节不进入模型上下文。
