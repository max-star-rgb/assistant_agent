# 34 Phase 4 发布检查清单

## 必须满足

- 至少一个真实 Provider Adapter 可选实现存在。
- 默认仍使用 MockAdapter。
- Integration tests 默认 skip。
- WebSocket 事件来自 runtime/event sink。
- 本地 TaskQueue 抽象存在。
- Memory 检索支持类型过滤与 Top-K。
- Failure Recovery Policy 存在并被 runtime 使用。
- Graph Execution Trace 可记录节点路径。
- API 响应带 protocol_version。
- 错误码结构统一。
- Eval 仍可离线运行。
- 测试默认不依赖外部服务。

## 检查命令

```bash
python scripts/check_env.py
python -m pytest
python scripts/run_evals.py
git status --short
```

## Phase 4 审计报告

最终生成：

```text
docs/35-phase4-architecture-review.md
```

报告包含：

1. 真实 Provider 接入状态。
2. WebSocket 长任务事件流。
3. TaskQueue 抽象。
4. Memory 检索策略。
5. Failure Recovery 策略。
6. Trace 能力。
7. API 协议版本。
8. Mock 与真实能力边界。
9. Phase 5 建议。
