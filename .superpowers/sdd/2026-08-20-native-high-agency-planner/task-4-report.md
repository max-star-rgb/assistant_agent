# Task 4 实施报告：确定性 admission 与共享 composition 事实

## Status

已完成。

## 实现

- 新增冻结的 `PlanningAdmissionPolicy.from_inventory(...)`：从显式 `Sequence[BaseTool]` 与
  `SkillCatalog` 复制出不可变 Tool 名集合和 governed Tool → Skill 映射。
- 扩展 `admit_native_plan(...)`，确定性校验：
  - Tool 必须存在于受信 inventory；
  - node `required_skill_ids` 必须属于 Planner 实际 active Skill；
  - governed Tool 的授权 Skill 必须同时存在于 Planner active IDs 和该 node 的
    `required_skill_ids`；
  - node/deliverable evidence ref 必须指向真实捕获 evidence；
  - deliverable producer 必须指向真实 node；
  - node 数、依赖引用、DAG 无环与最大依赖深度。
- planner 在 admission 前先捕获本轮真实 Tool evidence 和实际 active Skill，并使用闭包中的不可变
  policy；不读取用户文本，不包含旅行、酒店或其他领域规则。
- `build_planning_graph(...)` 支持窄无 Tool fixture 的空默认，但 production 与所有规划 Tool 测试均显式
  传入 tools/catalog；已删除 production 对 compiled graph `ToolNode.tools_by_name` 私有结构的反射。
- `AgentServerExecutionOwner.compose(...)` 单次加载 repo Skill catalog，并把同一实例和同一 Tool
  inventory 传给 fast agent 与 planning graph。
- 同步更新 runtime 与 Agent Server authority。

## TDD 证据

### RED 1：admission API/校验

命令：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-high-agency-planner/test_plan_admission.py
```

结果：collection error，`PlanningAdmissionPolicy` 尚不存在；符合预期缺口。

### GREEN 1

同一命令结果：`11 passed`。

### RED 2：composition 单次加载与共享对象

命令：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-high-agency-planner/test_plan_admission.py::test_production_composition_loads_and_shares_one_skill_catalog
```

结果：断言失败，实际 composition load 次数为 `0`；符合预期缺口。

### GREEN 2

同一命令结果：`1 passed`，并确认 fast/planning 收到同一 catalog 实例。

## 最终验证

- Feature：`28 passed in 4.75s`
- 完整 mock core：`49 passed in 6.44s`
- 相关文件 `ruff check`：通过
- `scripts/check_documentation_authority.py --repo-root .`：`valid: true`
- `git diff --check`：通过

Core invariant: unchanged；仅为既有 planning admission 的 feature 扩展，没有新增或修改 core
invariant。为保持既有 core Tool fixture 使用显式 inventory/catalog，仅最小调整了对应调用点。

Tests: 新增/更新 `tests/tdd/native-high-agency-planner` 临时 RED/GREEN；用户可手动删除整个目录，未自动
晋升 core。

## Concerns

- 仓库级 `ruff check .` 仍命中本任务未修改文件的既有错误：
  `scripts/run_system_multimodal_embedding_eval.py:18`（E402）。本任务相关文件定向 ruff 全部通过，未扩大
  scope 修改该脚本。
- 未调用真实 Provider；所有 pytest 均为 mock/offline。
- 按 Task 4 scope 未提前实现 scheduler/revision/repair。
