# Phase 5G Tasks：Video Understanding as External MLLM Capability

Phase 5G 从 Task 081 开始。该阶段只做轻量视频理解能力接入，不做视频模型工程。

## 执行顺序

```text
081 Phase 5G Video Understanding Roadmap
082 Video Understanding Request / Result / Adapter Baseline
083 Video Provider Adapter Skeleton and Safety
084 Video Multistep Integration
085 Video Smoke / Eval / API Coverage
086 Phase 5G Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockVideoUnderstandingAdapter。
- 默认测试不得调用真实 Video Provider。
- 默认 eval 不调用真实 Video Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实视频、视频帧、真实 Provider 输出样本或大文件。
- 不做自研视频模型。
- 不做复杂抽帧系统。
- 不做实时 WebRTC。
- 不做视频数据库或视频监控平台。
- 真实 Video Provider 只能由用户显式运行 smoke 脚本或 env-gated integration tests 触发。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
