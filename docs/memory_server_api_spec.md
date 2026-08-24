# Memory Backend 私有 HTTP 接入契约

最后更新：2026-08-24

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Mem0 与远端视觉 Memory Service 私有 HTTP adapter 子集的当前权威 |
| Owns | Mem0 recall/turn capture，以及远端视觉记忆 upload/task/query 请求和响应消费边界 |
| Does not own | 通用 Memory Server 协议、Graph memory 节点/快照、LangMem、Media-Agent WebSocket wire |
| 源码与 schema 入口 | `src/assistant_agent/memory/mem0/`、`src/assistant_agent/memory/remote_service.py` |
| 验证入口 | `docs/authority.toml` 中 `memory-server-api.verification` |
| 相邻 authority | Graph memory 架构见 [`memory-service-architecture.md`](memory-service-architecture.md) |

项目不定义通用 Memory Server 协议。本文件记录当前 Graph 实际消费的两个私有 adapter 子集：Mem0 OSS REST，
以及显式启用的远端视觉 Memory Service。其他 backend 不依赖本协议。

## 远端视觉 Memory Service

该能力只在 `provider_mode=real`、`MEMORY_BACKEND=langmem`、
`REMOTE_VISUAL_MEMORY_ENABLED=true` 且地址完整时启用。部署 URL 只来自本机未跟踪配置，不硬编码进源码。

### 视频段摄入

Media-Agent WebSocket 收到的独立 Annex-B H.264 帧同时进入实时 latest-wins 流水线和独立顺序归档 lane。
归档 lane 每 30 秒或连接关闭时把 H.264 remux 为本机临时 MP4，并通过带 TTL 的 opaque capability URL
供同一内网的远端服务拉取。

```http
POST /v1/media/upload
Content-Type: application/json

{
  "session_id": "<native-thread-id>",
  "user_id": "<authenticated-identity>",
  "files": [{
    "file_id": "<stable-segment-id>",
    "file_url": "http://<agent-host>/internal/memory-media/<opaque-token>",
    "filename": "<safe-name>.mp4",
    "media_type": "video",
    "start_time": "<ISO-8601>",
    "metadata": {}
  }]
}
```

请求体不携带 H.264、MP4 bytes、Base64 或本机路径。远端通过 HTTP GET 拉取 MP4；返回的 `task_id` 用于：

```http
POST /v1/tasks_status
Content-Type: application/json

{"user_id":"<authenticated-identity>","task_id":"<task-id>"}
```

只有 `completed` 才撤销 URL 并删除本机 MP4。完成切片在调度后台上传前先写入本机 SQLite manifest，Agent
Server lifespan 重启后以同一稳定 `file_id` 重新提交。归档、容量或远端依赖失败只降级历史视觉记忆，不影响
视频 ACK、实时视觉或 chat run。

### 视觉记忆召回

当前 `memory_recall` 内部并行执行 LangMem 文本召回和：

```http
POST /v1/memories/query
Content-Type: application/json

{
  "user_id": "<authenticated-identity>",
  "session_id": "<native-thread-id>",
  "query": "<latest-human-text>",
  "top_k": 8,
  "direct_answer": false
}
```

adapter 只消费 `results[]` 中 `type=text` 的非空 `content`。图片、Base64、URL、direct answer、远端身份字段
和原始响应都不进入 Graph state。视觉分支具有独立 timeout，任何失败规范化为空结果；LangMem 和当前 turn
继续。两路文本带来源标签合并后，仍受 `memory_context` 既有预算约束。

### 远端视觉服务身份

远端视觉请求的 `user_id` 来自 Agent Server 认证 principal，`session_id` 使用 native thread ID；客户端媒体
payload 不能覆盖这两个字段。当前 tokenless developer auth 只适用于受信内网，不能当作公网认证机制。

## Mem0 身份

召回节点从可信 Runtime 身份生成不透明 `user_id` 和 `agent_id`；写入请求还必须
携带由 `user_id + agent_id + session_id` 稳定生成的不透明 `run_id`。用户输入不能直接覆盖这些字段。

## Logical turn 召回

```http
GET /memories?user_id=<opaque>&agent_id=<opaque>
```

响应返回该身份下的长期记忆；只消费 `results` 数组中每条记录的 `id`、`memory`、`created_at` 和可选
`score`。每个 chat run 的 `memory_recall` 调用一次并将规范化结果冻结为当前 run 的
`memory_context`；Memory 失败恢复或同一 run resume 不重复请求。ContextBuilder 仍执行最终预算裁剪。

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
该调用只由 Agent Server 延迟调度的 `assistant-memory-v1` 后台 run 执行，不位于 chat 回答关键路径。Mem0 adapter 为原生 HTTP
`add` 使用至少 30 秒的 I/O timeout，不自动 retry。

响应只消费原生 `results` 中每条合法记录的 `id`、`memory` 和 `event`。支持的 event 为
`ADD`、`UPDATE`、`DELETE`；空数组表示没有提炼出长期记忆。memory text 只可进入显式启用的
本机观测 overlay，不进入 canonical trace 或普通日志。

## 错误语义

- recall 失败：节点写入 degraded 空 `memory_context` 和 `mem0_recall_failed`，当前 turn 继续。
- extract 失败或 timeout：后台 run 失败或降级结束；已完成的 Assistant 回复不受影响。
- 响应原文、URL、凭据和异常细节不进入模型上下文。
