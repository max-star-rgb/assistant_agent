# 如何使用 Phase 6 文档和 Skills

## 推荐策略

Phase 6 文档可以一次性放入仓库，但执行应按阶段串行：

```text
6A → 6B → 6C → 6D → 6E
```

## 放置方式

把压缩包内容复制到项目根目录：

```text
docs/
tasks/
skills/
prompts/
```

## 使用 6A Skill

```text
请使用 skills/phase6a-runner/SKILL.md，并执行 skills/phase6a-runner/prompts/run_to_end.md。

执行规则：
自动执行该阶段全部 task。
每个 task 完成后运行对应验收命令。
测试通过后继续下一个 task。
只做到该阶段 Review task。
Review 完成后停止。
不自动进入下一阶段。
默认 mock/local。
不调用真实 Provider。
不写入 API Key。
优先使用 apply_patch。
```

注意：`prompts/run_to_end.md` 指的是该 skill 目录下的 prompts 文件。

例如完整路径：

```text
skills/phase6a-runner/prompts/run_to_end.md
```

## 使用 6B / 6C / 6D / 6E
```text
请使用 skills/phase6b-runner/SKILL.md，并执行 skills/phase6b-runner/prompts/run_to_end.md。
请使用 skills/phase6c-runner/SKILL.md，并执行 skills/phase6c-runner/prompts/run_to_end.md。
请使用 skills/phase6d-runner/SKILL.md，并执行 skills/phase6d-runner/prompts/run_to_end.md。
请使用 skills/phase6e-runner/SKILL.md，并执行 skills/phase6e-runner/prompts/run_to_end.md。
```

## 不建议

不建议一次性自动跑完整 Phase 6。Phase 6 涉及用户入口、API、Web、部署和文档收敛，最好每个阶段审计后再继续。
