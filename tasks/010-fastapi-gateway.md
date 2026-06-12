# Task 010：FastAPI Gateway

## Goal

提供 `/agent/run` 和 `/health` HTTP 接口，把用户请求送入 Agent workflow。

## Read first

- `docs/07-service-api.md`
- `docs/01-architecture.md`
- `docs/08-testing.md`

## Scope

新增/修改：

```text
src/multimodal_agent/api/app.py
src/multimodal_agent/api/routes_agent.py
src/multimodal_agent/agent/workflow.py
tests/integration/test_api_agent.py
```

## Steps

1. 创建 FastAPI app。
2. 实现 `/health`。
3. 实现 `/agent/run`。
4. 实现最小 Agent workflow：intent → route → tools → response。
5. 使用 TestClient 编写集成测试。

## Acceptance

```bash
pytest tests/integration/test_api_agent.py
```

必须验证 `/agent/run` 能处理：

```json
{"text":"帮我找视频里的鞋子并比价","video_ids":["v1"]}
```

并返回结构化 tool_calls。

## Out of scope

- 不实现 WebSocket。
- 不部署服务。
