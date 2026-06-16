# Phase 5B Tasks：Text-first Capabilities

Phase 5B 从 Task 048 开始。该阶段聚焦两个最核心的纯文本能力：

```text
direct_chat
image_generation
```

不要在 Phase 5B 中接入商品搜索、比价、3D 渲染或新的 Vision hardening；这些能力保持 Phase 5A 的 routing baseline 状态。

## 执行顺序

```text
048 Phase 5B Text-first Capabilities Roadmap
049 Direct Chat Provider Adapter
050 Image Generation Provider Adapter
051 Prompt and Output Contracts
052 Text-only Image Generation Smoke
053 Text Capability Evals and API Coverage
054 Phase 5B Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockAdapter。
- 默认测试不得调用真实 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实生成图片。
- 真实 Provider 只能由用户显式运行 smoke 脚本或 env-gated integration tests 触发。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
