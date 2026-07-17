# 测试分层与路由

本文件是测试结构和运行方式的人类可读权威；`tests/scope-map.toml` 是源码到测试范围的
机器可读权威。旧的根目录、`unit`、`contracts`、phase、demo、eval 和 smoke 汇总布局已移除。

## 最终目录

```text
tests/
  critical/       # 裸 pytest 默认收集的跨 scope 安全底座
  scopes/
    prompt/
    context/
    tools/
    gateway/
    runtime/
    memory/
    providers/
    api/
  integration/    # 显式 opt-in；不属于默认 offline suite
    tools/        # tool-level 真实 Provider smoke；普通开发阶段不运行
  evals/          # 离线 eval case 数据；不是 pytest scope
  conftest.py
  scope-map.toml
```

critical 覆盖 Provider/offline 安全、Tool 治理、Memory read/write policy、Gateway 生命周期、
runtime 恢复、redaction 和测试路由。普通领域行为只进入一个权威 scope。高延迟、多进程或需要
外部环境的证据进入 integration。需要真实 Provider 的工具级 smoke 放在
`tests/integration/tools/`，仍按 integration 管理，普通开发阶段不运行。`tests/evals` 只保存离线
eval 数据，运行入口是 eval 脚本，不要作为 pytest scope 或旧式 eval 测试汇总目录使用。

## 命令

```bash
# 最快安全底座；裸 pytest 与此等价
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q

# 已知模块边界，可重复传入 --scope
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --scope tools -- -q

# 按已提交 Git range 自动选择 scope
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --changed BASE..HEAD -- -q

# critical 与全部 scope 的完整离线套件
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_scoped_tests.py --full -- -q
```

runner 强制 mock/offline profile、禁用 dotenv，并移除 integration opt-in。源码路径无法映射、
scope 不存在或为空时会保守失败。修改 `tests/**`、runner、scope map、conftest 或 pytest 配置
会选择所有 scope。

`--full` 只用于共享测试基础设施或 scope 路由变化、核心安全底座行为变化、三个及以上
scope、发布/合并门槛或用户明确要求。两个 scope 的普通功能只运行 critical 与受影响 scope，
普通局部开发不反复运行完整套件。

## Marker 语义

- `critical`：所有变更都必须守住的跨 scope 离线底座。
- `fast`：当前与 critical 同步，适合最短反馈循环。
- `unit`：隔离的模型、helper 或本地 service 行为。
- `contract`：adapter、tool 或协议边界契约。
- `api`：HTTP、WebSocket、CLI 或入口层行为。
- `runtime`：assistant loop、graph、gateway、realtime 或 routing 行为。
- `eval`：离线 eval 用例或 eval 脚本检查，不代表 pytest scope。
- `smoke`：operator-facing smoke 脚本或 smoke 检查。
- `slow`：不属于小改动快速路径的较宽回归检查。
- `integration`：显式 opt-in 的高延迟、多进程或外部环境检查。
- `e2e`：demo 或多层 workflow 检查；默认仍必须离线，真实 Provider 另走 opt-in。
- `regression`：必须说明具体历史缺陷或兼容目的的保护。

scope 决定“改了什么要跑什么”，marker 描述成本和层级，两者不能互相替代。默认测试不得联网
或调用真实 Provider。

## 新增测试方法

功能实现、缺陷修复和行为重构先使用 `.codex/skills/assistant-agent-development-testing`，在
`ADD`、`EXTEND`、`REUSE`、`STAGE`、`NO-TEST` 中选择主要决策。强制的是测试决策和验证证据，
不是新增测试数量。

STAGE 测试只允许在开发阶段暂存。阶段结束时，稳定契约必须去掉阶段命名后归入唯一 scope，
具名历史故障可归入 regression，重复或仅证明里程碑完成的测试必须删除；不得以 phase/stage
命名或待清理标记进入阶段提交。

每个新增测试必须说明并落实：

1. 保护的稳定契约或具名历史故障；
2. 现有测试未覆盖的边界；
3. 唯一的架构 scope 和最窄权威 layer；
4. 可直接定位的单一失败原因；
5. 时间、随机值、网络和全局状态的确定性处理。

执行规则：

- TDD：先观察正确原因的失败，再写最小实现并验证 GREEN。
- 只有跨模块不可缺少、快速、稳定、完全离线的契约才能进入 critical。
- 单测能证明的行为不再增加跨层重复；只有 wiring 无法在窄层证明时才增加离线端到端测试。
- 禁止新增 phase 编号、汇总型测试文件和真实 sleep；时间使用注入时钟或事件。
- 参数化只合并 setup、边界、失败模式和断言等价的用例，不能牺牲失败定位。
- 新 regression 必须替代旧测试时，在同一变更删除旧副本。
- 数量、覆盖率、年龄或耗时只能帮助排序，不能单独授权删除。
- 删除或合并必须命名保留测试，或明确说明被保护行为已不再受支持。

全仓审计、去重、分层、marker 治理或清理仅在用户明确要求时使用
`.codex/skills/assistant-agent-test-governance`。
