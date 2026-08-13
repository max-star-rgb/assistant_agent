# Mem0 Graph Backend 私有 HTTP 接入契约

最后更新：2026-08-13

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Mem0 graph backend 私有 HTTP adapter 子集的当前权威 |
| Owns | `Mem0Client` 使用的 recall、turn capture、identity filter、响应字段与错误语义 |
| Does not own | 通用 Memory Server 协议、Graph memory 节点/快照/ledger、LangMem |
| 源码与 schema 入口 | `src/assistant_agent/memory/mem0/`、`src/assistant_agent/memory/backends/mem0.py` |
| 验证入口 | `docs/authority.toml` 中 `memory-server-api.verification` |
| 相邻 authority | Graph memory 架构见 [`memory-service-architecture.md`](memory-service-architecture.md) |

项目不定义通用 Memory Server 协议。本文件只记录 Mem0 `memory_recall` / `memory_commit` 节点私有
`Mem0Client` 实际使用的 Mem0 OSS REST 子集；完整行为以 Mem0 官方 API 为准。其他 backend 不依赖本协议。

## 身份

召回节点从可信 Runtime 身份生成不透明 `user_id` 和 `agent_id`；写入请求还必须
携带由 `user_id + agent_id + session_id` 稳定生成的不透明 `run_id`。用户输入不能直接覆盖这些字段。

## Logical turn 召回

```http
GET /memories?user_id=<opaque>&agent_id=<opaque>
```

响应返回该身份下的长期记忆；只消费 `results` 数组中每条记录的 `id`、`memory`、`created_at` 和可选
`score`。新 logical turn 的 `memory_recall` 调用一次并将规范化结果冻结为 checkpoint
`memory_context`；resume/replay/default fork 不重新请求。ContextBuilder 仍执行最终预算裁剪。

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
    "source": "runtime_turn_ingestion",
    "source_turn": "<opaque>",
    "occurred_at": "<ISO-8601>"
  }
}
```

项目不设置 `infer=false`，不发送自定义 extraction prompt，不创建 core/daily 双记录。
该调用由 `memory_commit` 在回答发布后同步执行，并先经过最小 durable ledger。Mem0 adapter 为原生 HTTP
`add` 使用至少 30 秒的 I/O timeout，不自动 retry。

响应只消费原生 `results` 中每条合法记录的 `id`、`memory` 和 `event`。支持的 event 为
`ADD`、`UPDATE`、`DELETE`；空数组表示没有提炼出长期记忆。memory text 只可进入显式启用的
本机观测 overlay，不进入 canonical trace 或普通日志。

## 错误语义

- recall 失败：节点写入 degraded 空 `memory_context` 和 `mem0_recall_failed`，当前 turn 继续。
- commit 失败或 timeout：节点只写脱敏的 `MemoryCommitState`；已发布的 Assistant 回复不受影响。
- 响应原文、URL、凭据和异常细节不进入模型上下文。
