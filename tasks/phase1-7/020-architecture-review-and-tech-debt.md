# Task 020 架构审计与技术债清理

## Goal

完成 Phase 2 后进行一次结构审计，避免项目变成堆功能。

## Read first

- `AGENTS.md`
- `docs/00-doc-map.md`
- `docs/11-langgraph-integration.md`
- `docs/12-multistep-planning.md`
- 当前 src/ 和 tests/

## Scope

只做审计和小范围清理，不新增大功能。

## Requirements

检查并输出报告：

```text
docs/16-phase2-architecture-review.md
```

报告包含：

1. 当前真实实现了哪些能力。
2. 哪些仍是 Mock。
3. LangGraph 体现在哪些文件。
4. Tool 是否仍通过 Adapter 调用。
5. 是否有直接 Provider 调用泄漏到 Tool。
6. 是否有不该出现的 `__init__.py` 聚合导出。
7. 测试覆盖缺口。
8. Phase 3 建议任务。

可以做的小修：

- 删除未使用 import。
- 修复明显命名不一致。
- 补充少量文档链接。
- 不做大规模重构。

## Acceptance

```bash
python -m pytest
```

并生成：

```text
docs/16-phase2-architecture-review.md
```

## Stop condition

完成后停止，等待用户决定 Phase 3。
