---
name: assistant-agent-test-governance
description: Use only when the user explicitly requests an assistant_agent test audit, deduplication, layering or marker review, test-suite cleanup, or explicitly names this skill.
---

# Assistant Agent 测试治理

依据仓库证据治理测试。普通功能开发不得自动扩大为全仓测试审计。

## 工作流

1. 阅读 `AGENTS.md`、`tests/README.md` 和 `tests/scope-map.toml`；检查 dirty、staged 和任务定义的 range，不触碰无关改动。
2. 先以 `--profile none` 运行证据采集器。只有用户或任务给出有意义的 Git range 时才增加 `--git-range`。
3. 检查每个候选的层级、实际 marker、imports/targets、入站引用、last-touch、断言、fixture、失败模式和保留测试。采集器输出的完全重复项只是候选，不是删除授权。
4. 分类处理：
   - **保留**：唯一契约、安全边界、历史回归、兼容行为和失败/恢复证据。
   - **合并**：仅当同层测试的 setup、条件、失败模式和断言等价；保留小型可诊断用例，必要时参数化。
   - **重分类**：marker、路径或命名与实际层级/成本不一致，但受保护行为不变。
   - **删除**：行为已移除，或全部断言与边界已由明确命名的保留测试完整覆盖。
5. 证据不足的候选只报告，不修改。测试数量、覆盖率、年龄和耗时都不能单独作为删除理由。
6. 编辑前记录“候选 -> 保留测试”映射。编辑后同步 marker、共享 fixture/builder、`tests/scope-map.toml` 和 `tests/README.md`；不要制造难以定位失败的大型合并测试。
7. 修改后运行受影响测试、critical 与受影响 scope、证据采集器复查、Skill 校验和 `git diff --check`。只有跨 scope 高风险变更、发布/合并门槛、共享测试基础设施变更或用户明确要求时，才运行 `--full`。profiling 永远不得启用 integration 或真实 Provider。

## 证据采集器

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-test-governance/scripts/collect_test_evidence.py \
  --repo-root . [--git-range BASE..HEAD] [--profile none|fast|full-offline]
```

命令只向 stdout 输出一个 JSON 文档，不修改仓库文件。`fast` 运行 `-m fast`；
`full-offline` 运行 `-m "not integration"` 并强制 mock runtime profile。profile 失败记录在
`profile_run.pytest_exit_code`，不代表采集器自身失败。

局部治理验证使用 scoped runner：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_scoped_tests.py --scope SCOPE -- -q
```

符合全量触发条件时使用：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_scoped_tests.py --full -- -q
```

Skill 校验：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/assistant-agent-test-governance
```

## 删除门槛

只有明确命名保留测试，并证明以下条件全部成立时才能删除或合并：支持行为相同、边界与失败模式相同、没有独有断言或 fixture 语义、没有历史回归/兼容目的，且修改后离线验证通过。否则保留或只报告候选。

## 常见错误

| 错误 | 必须采取的处理 |
| --- | --- |
| 为达到数量或耗时目标而删除 | 指标只作为背景，不构成授权。 |
| 把相同 AST 当作相同价值 | 检查历史、fixture、层级和失败目的。 |
| 强制要求覆盖率或 mutation 工具 | 使用现有证据；百分比永远不能单独作为门槛。 |
| 把大量用例合并成一个测试 | 保留明确的失败定位和命名回归用例。 |
| profiling integration 测试 | 停止并改用 offline profile。 |
