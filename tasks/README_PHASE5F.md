# Phase 5F Tasks：Hybrid Intent Router & Planner Quality

Phase 5F 从 Task 074 开始。该阶段不扩展新 Provider，而是提升意图识别、任务规划、缺失输入识别和追问质量。

## 执行顺序

```text
074 Phase 5F Hybrid Intent Router Roadmap
075 IntentDecision Schema and Capability Validator
076 Rule Router Confidence Refactor
077 LLM Intent Router Adapter Skeleton
078 Planner Quality and Slot Filling
079 Intent Router Eval Comparison
080 Phase 5F Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认 router 必须仍为 rule。
- 不默认调用真实 LLM。
- 不默认调用真实 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不允许 LLM 直接执行工具。
- LLM 输出必须经过 schema 校验和 CapabilityValidator。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
