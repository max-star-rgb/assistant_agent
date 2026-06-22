# Task 019 Agent 评估集

## Goal

增加离线评估集，用于检查 intent、tool routing、multistep、memory 的质量。

## Read first

- `docs/15-agent-evaluation.md`
- 当前 tests 目录
- 当前 AgentWorkflow 或 LangGraph 入口

## Scope

新增最小 eval runner。

## Requirements

新增：

```text
tests/evals/
scripts/run_evals.py
```

至少包含 10 条 case：

- 2 条商品搜索
- 2 条比价
- 2 条图片生成
- 1 条 3D 渲染
- 1 条记忆检索
- 2 条多步任务

## Runner

`run_evals.py` 应输出：

```text
total
passed
failed
pass_rate
failed_case_ids
```

## Tests

可以新增轻量测试确认 eval 文件可解析。

## Acceptance

```bash
python scripts/run_evals.py
python -m pytest
```

## Stop condition

完成后停止，不要继续 Task 020。
