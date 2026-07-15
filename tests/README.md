# 测试分层与路由

本文件是测试结构和运行方式的人类可读权威；`tests/scope-map.toml` 是 scoped runner
使用的机器可读权威。第一阶段保留现有全量套件，不改变裸 `pytest` 的收集行为，同时为普通开发
提供更短的反馈路径。

## 日常运行

普通开发优先运行 critical bootstrap 与受影响 scope：

```bash
# 已知模块边界时，可重复传入 --scope
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --scope tools -- -q

# 按已提交 Git range 自动选择 scope
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --changed BASE..HEAD -- -q

# 显式运行保留的全量离线回归
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --full-legacy -- -q
```

runner 始终使用 mock/offline profile，移除 integration opt-in，并拒绝未映射的
`src/assistant_agent/**` 路径。无法判断范围时应补充 scope map 或显式选择测试，不静默退化为漏测。

当前 critical bootstrap 是 `tests/unit` 与 `tests/contracts`；`tests/critical` 只是后续迁移入口。
当前 scope 为 `prompt`、`context`、`tools`、`gateway`、`runtime`、`memory`、`providers`、`api`。

## 何时运行全量 Legacy

以下情况运行 `--full-legacy`：

- 变更跨越多个 scope 且共享行为难以局部证明；
- 修改 `tests/conftest.py`、scope map、runner、pytest 配置或共享 fixture/builder；
- 发布、合并主干前的最终门槛；
- 用户明确要求全量回归。

普通局部开发不因习惯而反复运行全量套件。当前 CI 和裸 `pytest` 行为保持不变，等 scoped
基线稳定后再单独决定是否切换默认入口。

## Marker 语义

- `fast`：适合局部开发循环的单元和契约检查。
- `unit`：隔离的模型、helper 或本地 service 行为。
- `contract`：adapter、tool 或边界契约。
- `api`：HTTP、WebSocket、CLI 或入口层行为。
- `runtime`：assistant loop、graph、gateway、realtime 或 routing 行为。
- `eval`：完全离线的评测样例。
- `smoke`：smoke 脚本和运维入口检查。
- `slow`：不适合小变更反馈路径的宽回归。
- `integration`：需要显式环境配置的 opt-in 检查。
- `e2e`：跨层业务流。
- `regression`：有明确历史缺陷或阶段行为价值的保护。

marker 描述成本和边界，不替代 scope。真实 Provider 测试仍只能通过显式 integration/profile
配置启用，默认测试不得联网。

## 新增与迁移测试的方法

- 先确定要保护的契约、边界或历史失败模式，再写能先失败的最小测试。
- 优先在最窄层验证；只有单测无法证明跨层 wiring 时才增加离线端到端测试。
- 每个测试保持单一、可定位的失败原因；不要把许多小测试合并成难诊断的大测试。
- 新测试必须进入一个明确 scope；只有跨模块不可缺少、快速、稳定且完全离线的契约才进入 critical。
- 迁移旧测试时应移动或重写并删除旧副本，不复制形成两份权威。
- 测试数量、覆盖率、年龄或耗时只能帮助排序，不能单独作为删除依据。
- 删除或合并必须命名保留测试，并证明断言、边界、失败模式和历史回归价值均已覆盖。

全仓测试审计、去重、分层、marker 治理或清理仅在用户明确要求时使用
`.codex/skills/assistant-agent-test-governance`。
