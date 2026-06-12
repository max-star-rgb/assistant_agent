# Phase 4.5 Tasks

Phase 4.5 是真实 Provider Smoke Test 阶段，不是完整 Phase 5。

## 执行顺序

```text
038 真实 Vision Provider Smoke Test 准备
039 Demo 数据与本地运行说明
040 Smoke Test 审计报告
```

## 执行规则

- 每次只执行一个 task。
- 默认仍使用 MockAdapter。
- 不自动调用真实外部 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含密钥的 `.env` 或 `.env.local`。
- 可以更新 `.env.example`。
- 可以新增 smoke 脚本，但脚本只有用户显式运行时才调用真实 Provider。
- `python -m pytest` 必须默认离线通过。
- 修改源码、测试、文档优先使用 apply_patch。

## 进入 Phase 5 的条件

完成 Phase 4.5 后，如果真实 Vision Provider smoke 跑通，再决定是否进入 Phase 5。

Phase 5 不应一次性做完，而应按真实使用中暴露的问题分块推进。
