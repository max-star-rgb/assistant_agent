# Phase 5A Tasks：Assistant Capability Routing Baseline

Phase 5A 从 Task 041 开始。该阶段主线不是 Vision Provider hardening，而是 Assistant Capability Routing Baseline。

## 业务定位

本项目是 Intent-driven Assistant Agent。Agent 应根据用户意图自主选择能力：

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

其中 `direct_chat` 和 `image_generation` 必须支持纯文本输入，不依赖图片或视频。

真实 Qwen Vision smoke 已经跑通，但它只是 Provider validation，不是 Phase 5A 主线。

## 执行顺序

```text
041 Assistant Capability Routing Roadmap
042 Intent Taxonomy and Capability Contracts
043 Text-only Routing Baseline
044 Media-aware Routing Baseline
045 Multi-step Orchestration Baseline
046 Routing Evals and Regression Suite
047 Phase 5A Assistant Routing Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockAdapter。
- 默认测试不得调用真实 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不提交真实图片、视频、大文件或真实 Provider 输出样本。
- 真实 Provider 只能由用户显式运行 smoke 脚本触发。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
