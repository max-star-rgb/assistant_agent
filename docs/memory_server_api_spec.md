# Memory Backend 私有 HTTP 接入契约

最后更新：2026-09-08

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | Mem0 与远端视觉 Memory Service 私有 HTTP adapter 子集的当前权威 |
| Owns | Mem0 recall/turn capture，以及远端视觉记忆 upload/task/query 请求和响应消费边界 |
| Does not own | 通用 Memory Server 协议、Graph memory 节点/快照、LangMem、Media-Agent WebSocket wire |
| 源码与 schema 入口 | `src/assistant_agent/memory/mem0/`、`src/assistant_agent/memory/remote_service.py` |
| 验证入口 | `docs/authority.toml` 中 `memory-server-api.verification` |
| 相邻 authority | Graph memory 架构见 [`memory-service-architecture.md`](memory-service-architecture.md) |

项目不定义通用 Memory Server 协议。本文件记录远端视觉服务与保留的旧 Mem0 adapter。
Gateway 文本记忆使用 Node 原生 Markdown，不调用以下 Mem0 HTTP 接口；旧数据不自动删除。

## 远端视觉 Memory Service

该能力只在 `provider_mode=real`、`REMOTE_VISUAL_MEMORY_ENABLED=true` 且地址完整时启用，
不依赖文本 MEMORY_BACKEND。部署 URL 只来自本机未跟踪配置，不硬编码进源码。

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

Gateway 视觉快照及保留的 Python hybrid backend 复用以下远端请求：

```http
POST /v1/memories/query
Content-Type: application/json

{
  "user_id": "<authenticated-identity>",
  "query": "<latest-human-text>",
  "top_k": 8,
  "direct_answer": false
}
```

adapter 只消费 `results[]` 中 `type=text` 的非空 `content`。图片、Base64、URL、direct answer、远端身份字段
和原始响应都不进入模型上下文。Gateway 通过受信回环接口 `/qwen-audio/visual-memory/snapshot`
POST 等待有界初始化召回、GET 读取进程内快照；身份来自认证 principal，客户端不能覆盖。
初始化及每成功处理完成 10 个视频段时以固定概括性 query 刷新，不依赖本轮用户问题；前台不暴露召回 Tool。
旧 Python hybrid 分支保留独立 timeout 和 fail-open 语义，仍受 memory_context 预算约束。

### 远端视觉服务身份

远端视觉上传请求的 `user_id` 来自 Agent Server 认证 principal，`session_id` 使用 native thread ID；客户端
媒体 payload 不能覆盖这两个字段。视觉查询请求只发送同一可信 `user_id`，有意不发送 `session_id`，以检索
该用户跨 thread 的长期视觉记忆；用户消息或客户端 payload 均不能补写或覆盖查询身份字段。当前 tokenless
developer auth 只适用于受信内网，不能当作公网认证机制。

## Mem0 身份

召回节点从可信 Runtime 身份生成不透明 `user_id` 和 `agent_id`；写入请求还必须
携带由 `user_id + agent_id + session_id` 稳定生成的不透明 `run_id`。用户输入不能直接覆盖这些字段。

## Logical turn 召回

```http
POST /search
Content-Type: application/json

{
  "query": "<latest-human-text>",
  "filters": {"user_id": "<opaque>", "agent_id": "<opaque>"},
  "limit": 32
}
```

请求不带 `run_id`，以召回同一用户跨会话的相关记忆；无用户正文时不请求。响应只消费 `results` 数组中每条记录的 `id`、`memory`、`created_at` 和可选
`score`。每个 chat run 的 `memory_recall` 调用一次并将规范化结果冻结为当前 run 的
`memory_context`；从召回之后的 checkpoint 恢复时复用快照，重新执行 recall 节点时允许重读。缺失或非数组
`results` 明确失败，不能伪装为空结果；空数组是合法的未命中。预算由 Memory backend 和 Context 投影约束。

## Conversation capture

```http
POST /memories
Content-Type: application/json

{
  "messages": [
    {"role": "user", "content": "<earlier-user-text>"},
    {"role": "user", "content": "<latest-user-text>"}
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
HTTP 输入包含后台快照中全部非空用户正文，不包含 AI、System、Tool 或媒体块；不新增增量游标。
`source_turn` 只标识提交来源，不保证服务幂等；重复快照交给 Mem0 整合。sidecar 保留已有的提取配置。
该调用只由 Agent Server 延迟调度的 `assistant-memory-v1` 后台 run 执行，不位于 chat 回答关键路径。Mem0 adapter 为原生 HTTP
`add` 使用至少 30 秒的 I/O timeout，不自动 retry。

响应只消费原生 `results` 中每条合法记录的 `id`、`memory` 和 `event`。支持的 event 为
`ADD`、`UPDATE`、`DELETE`；空数组表示没有提炼出长期记忆。memory text 只可进入显式启用的
本机观测 overlay，不进入 canonical trace 或普通日志。

## 显式 Mem0 编辑与受保护提取

Realtime 列表通过 `GET /memories?user_id=...&agent_id=...&limit=33` 获取，额外一项用于标识截断。
`POST /memory-edits` 接收可信 user_id/agent_id、operation_id 和严格 edit：append 携带 content，
replace 携带 memory_id/old_text/new_text。sidecar 在同一临界区复核记录身份与完整原文，
执行原文新增（infer=false）、更新或删除。空 new_text 表示删除。operation_id 在用户内唯一；
同 ID 不同参数返回 operation_conflict，持久化回执可由 `POST /memory-edits/status` 查询。
不确定的外部提交保持 outcome_unknown，不自动再执行。

`POST /memory-snapshots` 接收可信用户、source 和有界用户文本列表，返回 opaque reference。
`POST /memory-snapshots/extract` 按用户复核 reference，并在修订有效时调用原生 Mem0 add。
已被编辑取代的快照返回 superseded，空基线返回 skipped；错误不伪装为成功。
旧 `/memories` 的 runtime_turn_ingestion 在用户已有显式编辑后拒绝，防止旧 delayed run 绕过修订保护。
后台自动提取仍使用 infer=true；显式编辑的 infer=false 与自动提取分离。

edits.sqlite 与 history.db 同目录持久化；新模块由镜像 COPY 和 compose 挂载提供。
默认单进程 uvicorn 下共享串行锁。协调器重启保留修订与回执，不能通过增加 uvicorn worker 数扩容。

## 错误语义

- recall 失败：middleware 原生重试最多三次，耗尽后 chat run 明确 error，不静默降级。
- extract 失败或 timeout：独立节点原生重试最多三次，耗尽后后台 run 明确 error；已完成的 Assistant 回复不受影响。
- 响应原文、URL、凭据和异常细节不进入模型上下文。
