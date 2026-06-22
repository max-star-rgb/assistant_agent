# Phase 4 Tasks

Phase 4 接在 Phase 3 发布检查后执行。目标是生产化边界增强，但暂不引入 Harness 架构重命名。

## 执行顺序

```text
029 真实 Provider Adapter 可选实现
030 Provider Integration Tests 完善
031 长任务事件流与 WebSocket 升级
032 本地任务队列抽象
033 Memory 检索策略增强
034 失败恢复策略
035 Graph Execution Trace
036 API 错误码与响应协议版本
037 发布清理与 Phase 4 架构审计
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 默认使用 MockAdapter。
- 不自动安装依赖。
- 不自动调用真实外部服务。
- 不写入 API Key。
- 真实 Provider 仅在 env 配置完整且 `RUN_INTEGRATION_TESTS=1` 时启用。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 完成标准

每个 task 完成后至少运行：

```bash
python -m pytest
```

涉及 eval 时运行：

```bash
python scripts/run_evals.py
```
