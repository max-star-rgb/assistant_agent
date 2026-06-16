# 118 Phase 6C：Real Provider Opt-in Demo

## 目标

让用户可以选择性启用真实 Provider 进行本地 smoke / demo，但默认仍是 mock/local。

## 支持范围

优先支持已经有 skeleton 或 smoke 的 Provider：

```text
Vision Provider
Chat Provider
Image Generation Provider
Product Search Provider
Render Provider
Video Understanding Provider
```

Phase 6C 不要求全部接通真实服务，只要求 runbook、配置边界和 opt-in smoke 清晰。

## 关键原则

```text
默认 mock
显式环境变量才启用真实 Provider
缺配置时清晰提示
不提交 API Key
不提交真实输出
不影响默认 pytest/eval/demo
```

## 产物

```text
docs/provider-setup.md
docs/real-provider-smoke-runbook.md
.env.example 更新
scripts/smoke_* 统一说明
```

## 验收标准

- Provider 配置文档清晰。
- 每个真实 Provider 都有 opt-in 说明或明确暂缓原因。
- 默认测试不调用真实 Provider。
- 缺 key 不崩溃。
