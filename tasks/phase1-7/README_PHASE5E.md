# Phase 5E Tasks：End-to-End Demo Flow & Response Quality

Phase 5E 从 Task 067 开始。该阶段不新增真实 Provider，而是把已有能力串成端到端 demo flow，并改进回答质量。

Phase 5E 只聚焦：

- demo scenario matrix
- capability output contract unification
- template-based response composer quality
- eval suite layering
- offline E2E demo runner
- Phase 5E review

## 执行顺序

```text
067 Phase 5E E2E Demo Flow Roadmap
068 Demo Scenario Matrix
069 Capability Output Contract Unification
070 Response Composer Quality
071 Eval Suite Layering
072 E2E Demo Runner
073 Phase 5E Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockAdapter / LocalJsonAdapter。
- 默认测试不得调用真实 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交真实图片、视频、生成图片、渲染产物、大规模商品数据或真实 Provider 输出样本。
- 不接入新的真实 Provider。
- 不默认调用真实外部 Provider。
- 不升级复杂 LLM intent router。
- 不做 MCP Server 或 Skills 打包。
- 不实现 Hybrid LLM Intent Router。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
