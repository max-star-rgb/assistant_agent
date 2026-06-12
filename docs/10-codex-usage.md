# 10 如何让 Codex 使用这套文档

## 1. 核心思路

不要让 Codex 一次读完整长文档。使用：

```text
AGENTS.md       自动加载的稳定规则
docs/           按需阅读的架构说明
tasks/          单次执行的可验收任务
prompts/        可复制的 Codex 提示词
```

## 2. 第一次启动

在仓库根目录运行 Codex，然后输入：

```text
先阅读 AGENTS.md、docs/00-doc-map.md 和 tasks/README.md。
不要开始写代码。
请总结：项目目标、文档层级、任务顺序、MVP 边界。
```

## 3. 开始实现

```text
执行 tasks/000-project-scaffold.md。
实现前先列计划。
只做该任务范围。
完成后运行验收检查，并停止等待确认。
```

## 4. 后续推进

```text
继续执行下一个未完成任务。
先阅读该任务的 Read first 文档。
不要跨任务实现。
完成后更新总结，并告诉我下一个任务是什么。
```

## 5. 代码审查

```text
根据 AGENTS.md、docs/08-testing.md 和当前任务 Acceptance 审查本次改动。
指出 bug、缺失测试、跨任务实现和不符合架构的地方。
不要修改代码，先给审查报告。
```

## 6. 任务卡维护

每个任务完成后，在任务文件末尾追加：

```text
## Status

- [x] Implemented
- Test command: pytest ...
- Notes: ...
```

如果你不希望 Codex 修改任务文件，也可以让它只在回复里报告状态。
