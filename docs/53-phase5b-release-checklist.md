# 53 Phase 5B Release Checklist

## 必须满足

- direct_chat 支持纯文本输入。
- image_generation 支持纯文本输入。
- direct_chat 有 adapter contract。
- image_generation 有 adapter contract。
- PromptBuilder 或等价 prompt 构造逻辑可测试。
- 默认 provider 仍为 mock/local。
- 默认 pytest 不调用真实 Provider。
- 默认 eval 不调用真实 Provider。
- smoke 脚本必须用户显式运行才触发真实 Provider。
- `.env.example` 只有占位说明，无真实 key。
- 真实生成图片目录被 `.gitignore` 忽略。
- API 输出协议稳定。

## 检查命令

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Phase 5B 审计报告

最终生成：

```text
docs/54-phase5b-text-first-capabilities-review.md
```

报告包含：

1. Direct Chat 状态。
2. Image Generation 状态。
3. Prompt/output contract。
4. Mock 与真实 Provider 边界。
5. Smoke 能力。
6. Eval 覆盖。
7. 是否存在 key/data 泄露风险。
8. Phase 5C 建议。
