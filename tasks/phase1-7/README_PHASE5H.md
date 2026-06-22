# Phase 5H Tasks：Provider Safety / Retry / Cost / Trace Query

Phase 5H 从 Task 087 开始。该阶段不新增真实 Provider，而是对已有 Provider Adapter 增加横向安全能力。

## 执行顺序

```text
087 Phase 5H Provider Safety Roadmap
088 Provider Error Taxonomy and Safety Policy
089 Retry / Fallback / Timeout Policy
090 Provider Call Budget and Cost Guard
091 Trace Query and Redaction
092 Provider Safety Eval and API Coverage
093 Phase 5H Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockAdapter / LocalJsonAdapter。
- 默认测试不得调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实 Provider raw response、真实媒体、生成物、渲染产物或大文件。
- 不新增真实 Provider。
- 不实现 MCP / Skills。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
