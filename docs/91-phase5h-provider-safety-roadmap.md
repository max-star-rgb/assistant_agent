# 91 Phase 5H 路线图：Provider Safety / Retry / Cost / Trace Query

## 背景

Phase 5A 到 Phase 5G 已经逐步完成 Assistant Agent 的主要能力基线：

```text
direct_chat
image_generation
image_understanding
video_understanding
product_search
price_compare
render_3d
memory_retrieval
multi_step_orchestration
```

这些能力已经具备 Mock / Local / Provider Adapter 边界，并且大多数默认离线运行。

Phase 5H 不新增新 capability，也不接入新的真实 Provider。Phase 5H 是一个横向安全层，目标是让所有 Provider 调用都具备统一的：

```text
错误映射
超时控制
重试策略
降级策略
调用预算
成本估算
敏感信息脱敏
Trace 查询
API 调试能力
```

## Phase 5H 总目标

当未来启用真实 Provider 时，系统不应该因为单个 Provider 失败而崩溃，也不应该泄露 API Key、无限重试或产生不可控成本。

Phase 5H 的核心目标：

1. 统一 Provider 错误码。
2. 统一 Provider safety policy。
3. 增加 retry / fallback / timeout 策略。
4. 增加 per-run 调用预算和成本保护。
5. 增强 trace 查询 API。
6. 增强日志 / trace / error 的敏感信息脱敏。
7. 增加 Provider safety eval 和 API 覆盖。
8. 保持默认 mock/local-first。
9. 默认不调用真实 Provider。

## 覆盖范围

Phase 5H 的规则应覆盖所有可能调用外部能力的 provider：

```text
chat provider
image generation provider
image understanding provider
video understanding provider
product search provider
price compare provider
render provider
memory store provider
trace store provider
```

## 不做什么

Phase 5H 暂不做：

- 新增真实 Provider。
- 默认调用真实 Provider。
- 分布式任务队列。
- 生产级权限系统。
- 计费系统。
- 前端调试面板。
- MCP Server。
- Skills 打包。
- 大规模重构 LangGraph。
- 真实 API 压力测试。

## Provider Safety 核心原则

```text
Provider 可以失败，但 Agent 不能崩溃。
Provider 可以超时，但必须有结构化错误。
Provider 可以被限流，但不能无限重试。
Provider 可以收费，但必须有调用预算。
Provider 可以返回异常格式，但不能泄露 raw response。
Provider 可以被调试，但 trace 不能泄露密钥。
```

## Phase 5H 任务顺序

```text
087 Phase 5H Provider Safety Roadmap
088 Provider Error Taxonomy and Safety Policy
089 Retry / Fallback / Timeout Policy
090 Provider Call Budget and Cost Guard
091 Trace Query and Redaction
092 Provider Safety Eval and API Coverage
093 Phase 5H Review
```

## 默认安全边界

- 默认仍使用 MockAdapter / LocalJsonAdapter。
- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- 默认 demo runner 不调用真实 Provider。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实 Provider raw response。
- 不输出 Authorization header、Bearer token、完整 base64、大文件内容或隐私路径。
- 真实 Provider 只能由用户显式配置并手动运行 smoke 或 env-gated integration tests。

## Phase 5H 完成后

Phase 5H 完成后，可以考虑：

```text
Phase 5I Memory Hardening
Phase 5J MCP / Skills Packaging
Phase 6 Productization / UI / Deployment
```
