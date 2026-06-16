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
请使用 skills/phase6a-runner/SKILL.md，并执行 prompts/run_to_end.md。
```

注意：`prompts/run_to_end.md` 指的是该 skill 目录下的 prompts 文件。

例如完整路径：

```text
skills/phase6a-runner/prompts/run_to_end.md
```

## 使用 6B / 6C / 6D / 6E

对应替换 skill 名称：

```text
skills/phase6b-runner/SKILL.md
skills/phase6c-runner/SKILL.md
skills/phase6d-runner/SKILL.md
skills/phase6e-runner/SKILL.md
```

## 不建议

不建议一次性自动跑完整 Phase 6。Phase 6 涉及用户入口、API、Web、部署和文档收敛，最好每个阶段审计后再继续。
