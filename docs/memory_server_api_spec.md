# External Memory Service Interface (v1)

Last updated: 2026-07-22

Base URL: `http://<host>:<port>`

本文档是 assistant_agent 对外部 Memory Service / Memory Server 的当前 HTTP interface 权威。所有请求和响应均为 JSON；时间戳使用 ISO 8601 字符串。assistant_agent 本地记忆服务、治理边界和 adapter 接入规则见 `docs/memory-service-architecture.md`。

本文只定义 assistant_agent 需要依赖的外部接口 contract：endpoint、字段语义、兼容限制、错误形状和调用方约束。外部服务内部数据库、模型、Docker、GPU、embedding、抽取和 answer backend 实现不属于 assistant_agent 权威文档。

该 v1 Memory Server contract 与 framework/Mem0 sidecar 是两条不同集成路径。Mem0 使用其 OSS 原生无 `/v1` 接口：`POST /memories`、`POST /search`、`GET /memories/{id}` 等；assistant_agent 的请求映射、opaque identity、daily/core record 语义和 capture 生命周期以 `docs/memory-service-architecture.md` 为准，不在本文复制一套 Mem0 API。

## 0. Boundary

- 默认 mock/local/offline 运行不得因为存在 URL 或 credential 自动调用外部 Memory Service。
- 当前外部接口 contract 不声明生产级鉴权、配额、租户隔离或公网安全边界；接入方不得把它当作已认证的外部用户数据面。
- `assistant_agent` 侧必须通过 `MemoryManager`、`MemoryStore`/remote adapter、`MemoryMediaIngestionService` 和身份治理边界访问这些接口；媒体 ingestion 当前不是模型工具。
- 运行时身份以 assistant_agent 侧可信 `RequestIdentity` / `ToolContext` 为准；远端返回的 user/session 字段不能覆盖本地绑定身份。
- 外部接口 payload 不得把 raw provider response、base64/raw media、secret、token 或 credential 注入长期记忆内容、prompt、trace 或审计摘要。
- 当前 `file_id` 由调用方保证全局唯一；assistant_agent 侧媒体 ingestion service 会生成安全 `file_id`，避免复用用户输入作为持久主键。

## 1. Health

- Method: `GET`
- Path: `/v1/health`
- Description: 返回服务状态、当前 scope 下已加载 memory 数量，以及最新 memory timestamp。

### Query Parameters

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 否 | 限定 `memories_loaded` / `indexed_through` 到某个 user。 |
| `session_id` | string | 否 | 限定到某个 session；通常和 `user_id` 一起使用。 |

### Response

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 是 | `ok` 或 `degraded`。如果 DB schema 缺失，返回 `degraded`。 |
| `version` | string | 是 | 当前为 `0.1.0`。 |
| `memories_loaded` | integer | 是 | 当前 scope 下 memory 数量。 |
| `indexed_through` | string | 是 | 当前 scope 下最新 memory timestamp；没有数据时为空字符串。 |
| `code` | integer | 是 | 业务状态码，正常为 `200`。 |

```json
{
  "status": "ok",
  "version": "0.1.0",
  "memories_loaded": 2630,
  "indexed_through": "2024-04-23T18:30:00+00:00",
  "code": 200
}
```

## 2. Upload Media

- Method: `POST`
- Path: `/v1/media/upload`
- Description: 接收一个 upload task 中的一个或多个媒体文件，后台异步做 materialization、extraction、memory storage、keyframe generation 和 embedding。

### Request Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | Session 标识。 |
| `user_id` | string | 是 | User 标识。 |
| `files` | array | 是 | 本次 upload 的文件列表。 |

`files[]`:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | string | 是 | Client-provided file id。当前 `media_files.file_id` 是全局 primary key，调用方应保证唯一；这点后续可能调整。 |
| `file_url` | string | 是 | HTTP/HTTPS URL、本地路径或 `file://` URL。 |
| `filename` | string | 是 | 原始文件名，用于 materialized media basename。 |
| `media_type` | string | 是 | 媒体类型，例如 `video`、`audio`、`image`、`text`。 |
| `start_time` | string | 是 | 媒体在现实时间线上的开始时间。用于把相对 timestamp 转成绝对时间，也用于生成 task id 的时间前缀。 |
| `metadata` | object | 否 | 调用方自定义 metadata，默认 `{}`。 |

Extraction `file_id` guardrail：模型应把 label 中的 `file_id` 原样写回每条 memory item。服务端会接受真实 `file_id`、单文件缺失时的 inferred file id，以及唯一匹配 `filename`/basename 的 `aliased` file id；无法解析的 item 不落库为 memory，也不会生成 keyframe/embedding。

### Response `202`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | 异步 task id，格式约为 `{YYYYMMDDTHHMMSSZ}-{6hex}`。 |
| `status` | string | 是 | API 返回固定为 `processing`；DB 中 task 初始行会先是 `pending`，后台马上更新为 `processing`。 |
| `accepted_count` | integer | 是 | 接收的文件数量。 |
| `code` | integer | 是 | `202`。 |

```json
{
  "task_id": "20260411T120000Z-a1b2c3",
  "status": "processing",
  "accepted_count": 2,
  "code": 202
}
```

## 3. Task Status

- Method: `POST`
- Path: `/v1/tasks_status`
- Description: 查询 upload task 的后台处理状态。

### Request Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | 当前请求的 user id。注意：当前实现接受该字段，但 task lookup 只按 `task_id` 查询；user scope 尚未强制执行。 |
| `task_id` | string | 是 | Upload 返回的 task id。 |

### Response `200`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | string | 是 | Task id。 |
| `status` | string | 是 | `pending`、`processing`、`completed` 或 `failed`。 |
| `total_files` | integer | 是 | 本 task 文件数。 |
| `processed_files` | integer | 是 | 已处理文件数。 |
| `failed_files` | integer | 是 | 失败文件数。 |
| `estimated_completion_seconds` | number/null | 否 | 当前是 placeholder：`processing` 时固定 `1.0`，其他状态为 `null`。不要当作真实 ETA。 |
| `statistics` | object | 是 | 阶段耗时、memory breakdown、keyframe 状态、file_id resolution 等统计。 |
| `results` | array | 是 | Task 结果摘要。当前是 upload-level result，不是严格 per-file result。 |
| `errors` | array | 是 | 错误列表。 |
| `code` | integer | 是 | `200`。 |

### Response `404`

```json
{
  "error": "Task not found",
  "code": 404
}
```

## 4. Query Memories

- Method: `POST`
- Path: `/v1/memories/query`
- Description: 检索 memories，可选生成 direct answer。

### Request Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | User scope。 |
| `session_id` | string/null | 否 | 如果提供，只查该 session；如果缺失或为 `null`，查整个 user。 |
| `query` | string | 是 | 自然语言查询。 |
| `top_k` | integer/null | 否 | 返回 text memory 数量上限；默认来自 `retrieval.default_top_k`。注意最终 `results` 可能因 image chunks 超过 `top_k`。 |
| `direct_answer` | boolean | 否 | 是否调用 answer backend 生成自然语言回答，默认 `false`。 |
| `query_time` | string/null | 否 | 用户提问发生时间，用于解释相对时间；缺省时服务端取当前 UTC 时间。若未显式传 `before_timestamp`，默认用 `query_time` 作为可见性上界。 |
| `before_timestamp` | string/null | 否 | 只检索早于该时间的 memory；显式传入时优先于 `query_time` 默认上界。 |
| `after_timestamp` | string/null | 否 | 只检索晚于该时间的 memory。 |
| `options` | object/null | 否 | 查询选项。 |

时间语义：`query_time` 是回答视角，`before_timestamp` / `after_timestamp` 是检索硬过滤。未显式传 `before_timestamp` 时，服务端会默认使用 `query_time` 作为 visibility cutoff。启用 query planner 时，heuristic/LLM planner 可以在没有显式 request filter 时进一步推断 today/yesterday/recent/about N hours ago 等时间窗口。Direct answer prompt 会包含 effective `query_time`，让 answer backend 能把“刚刚 / 今天 / 上次 / about an hour ago”等相对时间解释为相对于提问时间。

`options`:

| 字段 | 类型 | 必填 | 当前行为 |
| --- | --- | --- | --- |
| `strategy` | string/null | 否 | `vector` 走 text embedding search；`hybrid` 走 text-vector + media-vector memory-level weighted RRF；默认 `long_context` 走时间顺序读取。没有生产 lexical/BM25 backend。 |
| `rerank` | boolean | 否 | 字段存在，但当前 QueryService 不使用。 |
| `memory_types` | array/null | 否 | 限定 memory type，例如 `episodic`、`semantic`、`procedural`、`spatial`。 |
| `embedding_model` | string/null | 否 | 字段存在，但当前 QueryService 不使用；实际使用启动时 wiring 的 embedding repo/embedder。 |
| `trace` | boolean | 否 | 请求级 debug trace 开关；默认 `false`。开启后 response 会包含 planner、branch、fusion、answer timezone 和 answer media 摘要。 |
| `timezone` | string/null | 否 | IANA timezone，例如 `Asia/Shanghai`。只影响 direct answer 中用户可见的绝对时间表达；不改变 server 存储时间、retrieval filters 或 `query_time` UTC 规划语义。缺省时使用 `answer.default_timezone`，默认 `Asia/Shanghai`（北京时间）。 |
| `text_retrieval_query` | string/null | 否 | 手动指定 text retrieval key；存在时跳过 planner 的 query rewrite。主要用于评测/调试。 |
| `media_retrieval_query` | string/null | 否 | 手动指定 media/image-text retrieval key；存在时跳过 planner 的 query rewrite。 |
| `answer_query` | string/null | 否 | 手动指定 answer prompt 中的用户问题；存在时跳过 planner 的 query rewrite。 |
| `include_media_chunks` | boolean/null | 否 | request 级覆盖是否把 parent-memory keyframe 作为 flat image chunks 返回。 |
| `answer_include_keyframes` | boolean/null | 否 | request 级覆盖 direct answer 是否发送 parent-memory keyframes。 |
| `answer_max_keyframes_per_memory` | integer/null | 否 | request 级覆盖每条 memory 的 parent-memory keyframe 上限。 |
| `answer_max_total_keyframes` | integer/null | 否 | request 级覆盖 direct answer 的 keyframe 总预算。 |
| `answer_include_media_vector_keyframes` | boolean/null | 否 | request 级覆盖 direct answer 是否使用 media-vector keyframe evidence。 |
| `answer_max_media_vector_keyframes` | integer/null | 否 | request 级覆盖 media-vector evidence 候选数量上限。 |

### Query Result Shape

`results[]` 是 `ScoredMemory`。默认配置 `query.include_media_chunks=true` 时，服务会在每个 text memory 后插入关联 keyframe image chunk；调用方如果只需要文本，应过滤 `type == "text"`。`strategy=hybrid` 时 primary ranking 是 memory-level weighted RRF，text result 的 `metadata` 会包含 `retrieval_source=hybrid`、`fused_score`、`branch_ranks` 等调试字段。带时间过滤的 query 会忽略 legacy/测试里缺 `timestamp_start` 的 memory；新 ingestion 路径会丢弃无法解析到可靠 timestamp 的 extraction item。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | `text` 或 `image`。 |
| `content` | string | 是 | Text memory 的文本；image chunk 当前为空字符串。 |
| `score` | number | 是 | 相关性分数。`long_context` 固定 `1.0`；vector 为 similarity score；substring fallback 为硬编码分数。 |
| `memory_type` | string | 是 | Text memory 的类型，或 image chunk 的 `keyframe`。 |
| `source` | object | 是 | 来源信息。Text result 使用 `memory_id`、`source_id`、`timestamp_start`、`timestamp_end`；parent image chunk 额外包含 `file_id`、`task_id`、`timestamp_offset_ms`、`timestamp_absolute`；media-vector image result 额外包含 `source=media_vector`、`media_index_id`。 |
| `media` | object/null | 否 | Image chunk 的媒体 payload，包含 `kind`、`url` 和/或 `base64`、timestamp 等。Text result 通常为 `null`。 |
| `image_url` | string/null | 否 | 兼容字段；text result 可来自 `Memory.frame_url`，image chunk 来自 keyframe URL。 |
| `image_base64` | string/null | 否 | 当 `query.media_chunk_return_mode` 为 `base64` 或 `both` 时可出现。 |
| `metadata` | object | 是 | Text result 通常包含 `topic` / `subtopic`；image chunk 包含 `index_id`、`index_type`、`selection_method`、`rank`、`score` 等。 |

### Direct Answer Behavior

`direct_answer=true` 时，QueryService 仍先检索 text results，然后组装 answer prompt。Answer 输出状态在 response 中通过 `answer_status` 表达：

| `answer_status` | 说明 |
| --- | --- |
| `null` | 没请求 direct answer。 |
| `no_results` | 没有 text results。 |
| `not_configured` | 请求了 direct answer，但 answer client 或 prompt 未配置。 |
| `success` | Answer backend 成功返回。 |
| `failed` | Answer backend 抛错；HTTP 仍返回 200，`answer` 是 fallback 文本，`answer_error` 是错误类型。 |

Keyframe evidence 由服务端配置控制，不是 request 字段：

- `answer.include_keyframes=false`（默认）：direct answer 只把 text memories 放进 prompt，不向 answer backend 发送图片。
- `answer.include_keyframes=true`：QueryService 会按命中的 text `memory_id` 查 `media_index.index_type=keyframe`，生成 parent-memory `AnswerMediaEvidence`，在 prompt 中为对应 memory 添加 `visual_evidence=keyframe-N` 引用，并把真实图片作为多模态输入传给支持的 answer backend。
- `answer.max_keyframes_per_memory` 限制每条 text memory 最多带多少个 parent-memory keyframe；`answer.max_total_keyframes` 可选地限制一次 answer 调用总共发送多少张 keyframe，避免 `top_k` 较大时把过多图片塞给 answer backend。
- `answer.include_media_vector_keyframes=true`：QueryService 会使用 media vector retrieval 命中的 keyframe 作为 answer evidence 候选，并把真实图片传给 answer backend。该路径依赖 `media_embeddings.enabled=true` 以及 media embedding repo/embedder wiring。
- Answer 图片选择是预算式的：先按 text/hybrid memory rank 做 coverage pass，每个 memory 优先选择 media-vector 命中的 keyframe；预算未满且 per-memory cap 允许时再 refill。
- 该路径与 `query.include_media_chunks` 解耦：query response 可以不返回 image chunks，但 direct answer 仍可带 keyframe；反之亦然。
- 不可读本地图片或当前 backend 不支持的 URI 会被跳过，避免 prompt 中出现悬空 visual evidence label。

### Response `200`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `answer` | string | 是 | Direct answer 文本；未请求时为空字符串。 |
| `answer_status` | string/null | 否 | 见上表。 |
| `answer_error` | string/null | 否 | Answer backend 失败时的错误类型。 |
| `results` | array | 是 | Text memories 加可选 image chunks。 |
| `total_results` | integer | 是 | `len(results)`，因此包含 image chunks。 |
| `query_time_ms` | integer | 是 | 总查询耗时，最小为 `1`。 |
| `timings` | object | 是 | 阶段耗时，例如 `retrieval_ms`、`embedding_ms`、`answer_ms`、`total_ms`。 |
| `external_calls` | array | 是 | Answer backend 等外部调用的摘要事件。 |
| `indexed_through` | string | 是 | 当前 query scope 下最新 memory timestamp；无数据时为空字符串。 |
| `trace` | object/null | 否 | 仅在 `query.return_trace=true` 或 `options.trace=true` 时包含；不含 embedding/full prompt。`trace.plan` 包含 query/key、`query_time`、memory type/time filters、`temporal_intent`、`metadata_filters` 和 planner warnings。 |
| `code` | integer | 是 | `200`。 |

```json
{
  "answer": "Jake had coffee and toast for breakfast.",
  "answer_status": "success",
  "answer_error": null,
  "results": [
    {
      "type": "text",
      "content": "Jake had breakfast with coffee and toast.",
      "score": 1.0,
      "memory_type": "episodic",
      "source": {
        "memory_id": "4fe0f7d7-0d0b-4c76-91b5-11d2b165f6c1",
        "source_id": "20260411T120000Z-a1b2c3",
        "timestamp_start": "2026-04-11T12:00:03+00:00",
        "timestamp_end": "2026-04-11T12:00:06+00:00"
      },
      "media": null,
      "image_url": "file:///data/derived/u1/20260411T120000Z-a1b2c3/keyframes/video1/4500.jpg",
      "image_base64": null,
      "metadata": {"topic": "breakfast", "subtopic": null}
    },
    {
      "type": "image",
      "content": "",
      "score": 1.0,
      "memory_type": "keyframe",
      "source": {
        "memory_id": "4fe0f7d7-0d0b-4c76-91b5-11d2b165f6c1",
        "source_id": "20260411T120000Z-a1b2c3",
        "file_id": "video1",
        "task_id": "20260411T120000Z-a1b2c3",
        "timestamp_offset_ms": 4500,
        "timestamp_absolute": "2026-04-11T12:00:04.5+00:00"
      },
      "media": {
        "kind": "keyframe",
        "url": "file:///data/derived/u1/20260411T120000Z-a1b2c3/keyframes/video1/4500.jpg",
        "base64": null,
        "timestamp_offset_ms": 4500,
        "timestamp_absolute": "2026-04-11T12:00:04.5+00:00"
      },
      "image_url": "file:///data/derived/u1/20260411T120000Z-a1b2c3/keyframes/video1/4500.jpg",
      "image_base64": null,
      "metadata": {
        "index_id": "9c25a9de-7e18-4c8a-9e2a-7b1892a5f123",
        "index_type": "keyframe",
        "selection_method": "memory_midpoint",
        "rank": 0,
        "score": null
      }
    }
  ],
  "total_results": 2,
  "query_time_ms": 45,
  "timings": {"retrieval_ms": 2, "answer_ms": 40, "total_ms": 45},
  "external_calls": [
    {"provider": "gemini", "operation": "models.generate_content.answer", "status": "success", "attempts": 1}
  ],
  "indexed_through": "2026-04-11T12:00:06+00:00",
  "code": 200
}
```

## 5. Add Session History

- Method: `POST`
- Path: `/v1/sessions/add_history`
- Description: 向 session history 追加对话或事件记录。Service 会尝试去掉 incoming entries 中已经存在的最长重复前缀。

### Request Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | User id。 |
| `session_id` | string | 是 | Session id。 |
| `entries` | array | 是 | 要追加的 history entries。 |

`entries[]`:

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `role` | string | 是 | `user`、`assistant`、`system` 或自定义角色。 |
| `content` | string | 是 | 消息内容或事件描述。 |
| `timestamp` | string/null | 否 | 如果缺失，服务会使用当前时间。 |
| `metadata` | object | 否 | 任意 metadata，默认 `{}`。 |

### Response `200`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 是 | `ok`。 |
| `session_id` | string | 是 | Session id。 |
| `entries_stored` | integer | 是 | 本次实际新增条目数。 |
| `total_entries` | integer | 是 | 该 session 当前总条目数。 |
| `code` | integer | 是 | `200`。 |

## 6. Get Session History

- Method: `POST`
- Path: `/v1/sessions/get_history`
- Description: 获取某个 session 或整个 user 的 session history。

### Request Body

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | User id。 |
| `session_id` | string/null | 否 | 如果提供，只返回该 session；如果缺失，返回 user scope 下的合并 history。 |
| `limit` | integer/null | 否 | 返回最新 N 条；response 中仍按时间正序排列。省略时使用 `sessions.history_default_limit`，默认 `80`；显式传 `null` 表示不限制。 |
| `session_limit` | integer/null | 否 | 未指定 `session_id` 时生效，只从最近活跃的 N 个 session 取 history。省略时使用 `sessions.history_default_session_limit`，默认 `3`；显式传 `null` 表示不限制。 |
| `before` | string/null | 否 | 只返回早于该 timestamp 的 history。 |
| `include_debug` | boolean | 否 | 默认 `false`。为 `true` 时 response 增加 `debug`，用于排查有效 limit、选中 sessions、entry id、内容长度和内容 hash。 |

### Response `200`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 是 | 如果 request 未指定 `session_id`，当前返回空字符串。 |
| `entries` | array | 是 | History entries。 |
| `total_entries` | integer | 是 | 当前 scope 的 entry 数量。 |
| `created_at` | string | 是 | Session 创建时间；多 session scope 下为选中 sessions 的最早创建时间。 |
| `debug` | object/null | 否 | 仅当 `include_debug=true` 时返回。包含 `effective_limit`、`effective_session_limit`、`returned_entries`、`total_entries`、`session_ids` 和每条 entry 的 `id/session_id/role/timestamp/content_length/content_sha256/metadata_keys`。 |
| `code` | integer | 是 | `200`。 |

## 7. Deprecated / TBD

旧文档中的“加载长期记忆”等接口当前没有实现。
