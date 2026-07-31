# Incubating system checks

这里保存具体节点、Provider adapter、业务能力和旧实现的开发期专项检查。它们不属于默认 pytest，
不属于发布门禁，也不等同于正式 system eval。文件统一命名为 `checks_*.py`，只能通过显式路径运行。

每个 feature 目录必须声明：

- Scope
- Mode: offline | real
- Command
- Side effects and gates
- Delete when
- Promote when

offline 检查必须使用 mock、local 或 in-memory 依赖，不读取真实 `.env`，不调用真实 Provider 或外部
服务。real 检查必须位于明确声明 real mode 的独立入口中，要求完整配置，并由 operator 传入对应
`--allow-*` 确认参数；不得从 real 静默回退 mock。

当对应事实已由稳定的正式 system runner、Agent eval Experiment 或生产证据覆盖后，可以手动删除整个
feature 目录，且不应修改 `tests/core` 或默认 pytest 配置。
