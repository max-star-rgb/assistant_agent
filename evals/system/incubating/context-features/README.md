# Context feature checks

- Scope: tokenizer、compiled accounting、rolling compaction 与 finalization/prompt projection 的旧实现检查。
- Mode: offline
- Command: `/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q evals/system/incubating/context-features/checks_*.py`
- Side effects and gates: 只使用确定性 tokenizer、mock Provider 与本地状态；不读取真实 `.env`，不调用真实模型。
- Delete when: 核心 Context 不变量与正式 context capture/system eval 已稳定覆盖相关事实。
- Promote when: 真实模型 context 行为进入显式 real mode、完整配置及 operator `--allow-unredacted-context` 门禁的正式 system eval。
