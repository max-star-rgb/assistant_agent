# 36 Phase 4.5 真实 Provider Smoke Test 设计

## 定位

Phase 4.5 不是完整 Phase 5，也不是生产部署阶段。

它的目标是：在不破坏默认 Mock/离线测试的前提下，用一个真实 Provider 做最小链路验证。

推荐优先验证：

```text
真实 Vision Provider
```

原因：视觉理解是多模态 Agent 的入口能力，验证价值最高；同时 Phase 4 已经有 `HttpVisionProviderAdapter`、ProviderConfig、Integration Test Gate 和结构化错误边界。

## 核心原则

```text
默认仍是 Mock
显式环境变量才启用真实 Provider
默认 pytest 不调用外部服务
API Key 不进代码、不进文档、不进 Git
真实数据先用低风险样例
```

## 与 Phase 5 的区别

Phase 5 是生产化加固，包括鉴权、用户隔离、成本控制、异步 worker、持久化 trace、权限系统等。

Phase 4.5 只做 Smoke Test：

- 检查真实 Vision Provider 是否能被环境变量启用。
- 检查缺少配置时是否清晰失败。
- 检查默认测试是否仍离线。
- 检查真实请求能否通过 AgentGraphRuntime 跑通。

## 推荐流程

```text
准备 .env.example
  ↓
准备低风险 demo data
  ↓
新增 smoke 脚本
  ↓
默认测试仍走 Mock
  ↓
用户本地手动设置 API Key
  ↓
用户显式运行 smoke 脚本
  ↓
观察 response / trace / events
```

## 不做事项

- 不自动安装依赖。
- 不自动写入 API Key。
- 不默认调用真实 Provider。
- 不提交 `.env`、`.env.local`。
- 不上传隐私图片、合同、身份证、人脸、公司内部资料。
- 不接真实图片生成、商品搜索、渲染 Provider，除非后续单独开 task。

## 成功标准

- `python -m pytest` 默认离线通过。
- `.env.example` 清楚列出真实 Vision Provider 需要的环境变量。
- smoke 脚本在缺少 API Key 时给出清晰提示并退出。
- 用户显式设置环境变量后，可以手动运行真实 Vision Provider smoke。
- 不泄露 API Key。
