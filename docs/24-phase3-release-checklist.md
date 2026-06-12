# 24 Phase 3 发布检查清单

## 目标

Phase 3 完成后，项目应从 MVP 骨架进入可维护 Agent Runtime 阶段。

## 必须满足

- LangGraph 是默认 Agent Runtime。
- API 默认走 graph runtime。
- AgentWorkflow.run() 保持兼容。
- Graph nodes 不依赖 workflow 私有方法。
- MemoryStore 可配置。
- JSONL memory 能跨重启读取。
- Adapter contract tests 默认运行。
- Integration tests 默认 skip。
- Eval cases 至少 30 条。
- 多步任务由 LangGraph loop 驱动。
- 项目内无明显缓存/构建产物污染。

## 检查命令

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## 审计输出

Phase 3 最后生成：

```text
docs/25-phase3-architecture-review.md
```

包括 Runtime 入口、Graph 文件、Node 边界、Memory 后端、Provider 测试体系、Eval 指标和 Phase 4 建议。
