# 117 Phase 6B：FastAPI Demo & Simple Web Console

## 目标

把 Agent 暴露为稳定的本地 HTTP demo，并提供一个简单 Web 控制台用于演示。

## 核心产物

```text
FastAPI demo endpoints
GET /health
POST /agent/run
GET /demo/scenarios
GET /runs/{run_id}
GET /traces/{trace_id}
Simple static web console or minimal HTML page
```

## Web Console 最小功能

```text
输入文本
选择 demo scenario
可选填写 image_ref / video_ref
点击运行
显示 response_text
显示 tool_calls
显示 trace_id
显示 errors
```

## 不做什么

- 不做复杂前端框架。
- 不做登录系统。
- 不做生产权限。
- 不做公网部署。
- 不默认调用真实 Provider。

## 验收标准

- FastAPI 启动可用。
- demo scenarios 可通过 API 列出。
- Web console 可触发一次 agent run。
- trace/run 查询可用。
- 默认离线。
