# 07 API 与服务接口

## 1. FastAPI 入口

MVP 提供三个核心接口：

```text
POST /agent/run
GET  /agent/runs/{run_id}
GET  /health
```

后续扩展：

```text
POST /tools/vision/understand
POST /tools/product/search
POST /tools/price/compare
POST /tools/image/generate
POST /tools/render/create
POST /memory/search
POST /memory/save
WS   /ws/agent/{session_id}
```

## 2. POST /agent/run

请求：

```json
{
  "user_id": "u1",
  "session_id": "s1",
  "text": "帮我找视频里的鞋子并比价",
  "image_ids": [],
  "video_ids": ["v1"]
}
```

响应：

```json
{
  "run_id": "run_123",
  "status": "completed",
  "intent": "multi_tool_task",
  "response_text": "我识别到视频中的白色低帮运动鞋，并找到 3 个相似商品...",
  "tool_calls": []
}
```

## 3. WebSocket 事件

用于长任务进度：

```json
{"type": "tool_started", "tool_name": "render_3d", "run_id": "run_123"}
{"type": "tool_progress", "tool_name": "render_3d", "progress": 0.5}
{"type": "tool_completed", "tool_name": "render_3d", "output_ref": "render_1"}
{"type": "agent_response", "text": "渲染完成。"}
```

## 4. API 设计原则

- API 层只负责收参、鉴权、调用 Agent、返回结果。
- 不在 API 层写业务决策。
- 所有请求和响应使用 Pydantic。
- 长任务返回 task_id/run_id，不阻塞 HTTP。
