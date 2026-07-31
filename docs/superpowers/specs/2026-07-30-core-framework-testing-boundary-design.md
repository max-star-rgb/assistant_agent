# 核心框架测试边界设计

## 1. 背景

当前默认 pytest 收集整个 `tests/`，同时覆盖核心 Runtime、Gateway、Tool 治理、具体业务 Tool、
Provider adapter、Agent eval 基础设施和逐 Task 资产。新增节点或专项功能经常带来新的 pytest，
导致默认套件持续增长、与实现细节和自然语言文案耦合，并使无关节点改动影响框架验证结果。

本设计将默认 pytest 收敛为稳定的核心框架安全网。具体节点、业务能力、真实 Provider 和模型行为
不再进入默认 pytest；功能开发期间允许在可手动删除的 `tests/tdd/<feature>/` 做临时 RED/GREEN，
确有长期验证价值的节点专项检查进入可独立删除的 system eval 目录。

## 2. 目标

- 裸 `pytest` 只验证核心框架不变量。
- 新增 Tool、Provider、业务节点或 Agent Task 时，不需要修改核心测试。
- 核心测试只断言稳定、结构化、与业务无关的框架行为。
- 默认阻止 Codex 为小功能、wrapper、配置字段、文案或覆盖率随意增加 pytest。
- 允许用户在 `tests/tdd/<feature>/` 保留显式运行、默认不收集的临时 RED/GREEN pytest。
- 功能开发阶段确有必要的专项检查能够独立运行，并可在项目稳定后整目录删除。
- 真实外部能力和模型行为继续通过 system eval 与 Agent eval 验证。

## 3. 非目标

- 不追求源码行覆盖率。
- 不要求每个模块、函数、Tool 或 Provider 都有 pytest。
- 不把所有现有 pytest 原样迁移到新目录。
- 不用核心 pytest 验证完整回复、prompt、Tool description、控制台输出或供应商 payload。
- 不允许通过 marker 把一个持续膨胀的全量 pytest 伪装成核心安全网。

## 4. 总体架构

```text
tests/
  README.md
  core/
    INVARIANTS.md
    conftest.py
    unit/
    contract/
    integration/
  tdd/
    README.md
    conftest.py
    <feature>/
      test_*.py

evals/
  system/
    incubating/
      <feature-name>/
        README.md
        checks.py
        cases.json        # 可选
```

`pyproject.toml` 的 `testpaths` 固定为 `tests/core`。裸 `pytest` 不收集 `tests/tdd` 或
`evals/system/incubating`。TDD 与 incubating 检查只能通过显式路径或对应目录自己的 runner 运行。

`unit`、`contract` 和 `integration` 仍表达测试形态，但只有位于 `tests/core/` 内、能够映射到已登记
框架不变量的测试才属于默认 pytest。

## 5. 核心框架不变量

初始核心不变量分组如下，实施时在 `tests/core/INVARIANTS.md` 中分配稳定 ID：

| 分组 | 必须保护的行为 |
| --- | --- |
| Bootstrap | mock/real 边界、离线初始化、真实能力不得静默回退 |
| Run lifecycle | request、run、完成、失败、取消和终态一致性 |
| Assistant loop | 纯文本完成、通用 tool call 循环、失败返回和迭代上限 |
| Tool governance | `ActionValidator -> ToolExecutor -> ToolRegistry -> tool` 不可绕过 |
| Extension contract | 通用 Probe Tool/Plugin 的注册、暴露、验证和执行 |
| Context | budget、compaction、tool call/result 因果配对等结构化机制 |
| Gateway | session、run、cancel、replace、reconnect 和稳定 frame 映射 |
| Identity | user、session、agent、run 的隔离 |
| Durable mechanism | 通用 schedule、resume、cancel、outbox 状态机 |
| Observability | canonical event、trace correlation 和终态可见性 |

以下内容没有资格进入核心测试：

- shopping、weather、email、calendar、lodging、file、image 等具体节点；
- 具体 Provider payload、模型名称、版本或供应商行为；
- Agent Task、grader、calibration、suite 和 Langfuse 展示细节；
- CLI 或控制台展示文案；
- 完整自然语言回复、prompt、Tool description；
- 简单 wrapper、配置常量、第三方框架自身行为和假设性边缘场景；
- 仅用于提升覆盖率的测试。

核心测试使用通用 `ProbeTool`、`ScriptedChatAdapter`、in-memory store 和无语义
`*-sentinel`。核心测试不得依赖具体 builtin Tool 或 Task。

## 6. 核心测试准入门禁

### 6.1 不变量登记

`tests/core/INVARIANTS.md` 保存稳定 ID、契约描述和负责测试文件。示例：

```text
BOOT-001  mock/real Provider 边界
RUN-001   Run 终态与取消语义
TOOL-001  Tool 治理执行链
GATE-001  Gateway frame 映射
IDENT-001 身份隔离
```

每个 core pytest item 必须通过 marker 关联已登记 ID：

```python
@pytest.mark.core_invariant("TOOL-001")
def test_probe_tool_runs_through_governed_chain() -> None:
    ...
```

`tests/core/conftest.py` 在收集时拒绝未标记或使用未知 ID 的测试。新增核心测试文件必须先登记其负责的
不变量；相同不变量优先扩展已有文件，不为单个 bug 创建长期文件。

### 6.2 目录门禁

默认收集固定为：

```toml
[tool.pytest.ini_options]
testpaths = ["tests/core"]
```

核心策略检查只允许永久 pytest 位于 `tests/core/`，并允许临时 pytest 位于
`tests/tdd/<feature>/`。它拒绝 `tests/tdd/` 根目录测试，以及 `tests/feature`、`tests/legacy`
等其他旁路目录。只有 core 测试需要登记 invariant ID 和 marker；TDD 测试不进入默认收集，也不会
自动晋升为 core。

### 6.3 文案和实现耦合门禁

核心策略检查拒绝：

- assert 中出现包含空格或中文的完整自然语言字符串；
- 对 message、text、content、prompt、description、stdout 或 stderr 做完整文案比较；
- 导入具体 builtin Tool、具体 Provider 实现、`evals.agent` Task 或 grader；
- 以私有方法调用次数作为主要正确性证据。

允许精确断言稳定协议 token、结构化字段、error code、枚举值和 `*-sentinel`。

### 6.4 临时 TDD 区

`tests/tdd/<feature>/` 只服务功能开发期间的 RED/GREEN，必须显式运行
`python -m pytest -q tests/tdd/<feature>`，并由 `tests/tdd/conftest.py` 强制 mock/offline。
Codex 不得擅自删除这些 feature 目录；功能完成后用户可手动整目录删除，或明确要求 Codex 删除。
临时 TDD 不是核心测试候选池，不会因存在时间或测试数量自动晋升。

## 7. System eval 与可删除检查

### 7.1 正式 system eval

稳定的真实 Tool、Provider、Context 或 Memory 能力继续位于正式
`evals/system/<domain>/`。真实运行必须保持 real mode、完整配置和 operator 显式确认门禁。

### 7.2 Incubating 检查

节点开发阶段确有必要、但不属于核心框架的确定性或真实专项检查放入：

```text
evals/system/incubating/<feature-name>/
```

每个目录保持自包含，不在根 `scripts/` 增加临时入口。其 `README.md` 必须说明：

- 所属功能和验证目标；
- 运行命令；
- offline 或 real 运行模式；
- 外部副作用和安全门禁；
- 删除条件；
- 成为长期系统能力时的晋升路径。

incubating 检查不属于发布默认门禁，不要求永久维护。功能稳定且已有正式 system eval、Agent eval
或生产证据后，可以整目录删除，不需要改动核心测试或根 pytest 配置。

### 7.3 Agent eval

模型是否选择正确节点、工具参数是否符合任务语义、回答是否 grounded 和有用，继续由
`evals/agent` 的 Task、Environment、Evidence 和 Grader 负责。逐 Task 元数据、calibration 和
模型行为不得回流默认 pytest。

## 8. 现有测试迁移

迁移不是机械移动。每个现有测试文件按以下顺序处理：

1. 判断它保护的是框架不变量还是具体节点。
2. 对框架不变量去重并合并进 `tests/core` 的聚焦文件。
3. 对仍处于开发期且具有专项验证价值的节点测试，迁入对应 incubating 目录。
4. 对已经由 system/Agent eval 覆盖的节点测试，不保留重复 pytest。
5. 删除完整文案、配置常量、私有实现细节、重复主链路和无风险证据的测试。

初始分类原则：

| 处理 | 当前测试类型 |
| --- | --- |
| 合并进 core | safety net、通用 Tool 治理、Gateway 生命周期、身份隔离、通用 Context/Durable/Trace 机制 |
| 迁入 incubating | shopping、lodging、email、file、weather、image、具体 Provider adapter、Agent Task、eval 控制面 |
| 删除 | 重复主链路、完整文案断言、实现细节断言、配置常量、已有等价 eval 的测试 |

实施前生成逐文件迁移清单。当前未提交测试不得直接丢失：迁移、删除前先确认其等价核心不变量或
专项 eval 归属。只处理测试体系和相关权威文档，不回滚相邻源码与 eval 功能改动。

## 9. 后续 Codex 决策规则

项目测试 skill 的默认结论改为“不新增测试”，决策顺序固定为：

1. 本次变更是否修改已登记核心不变量？
2. 如果没有，禁止向 `tests/core` 添加测试。
3. 如果只是具体节点、Provider、业务逻辑或文案变更，永久测试默认不新增；确需 TDD 时只进入
   `tests/tdd/<feature>/`，显式运行并保持可整目录删除。
4. 存在真实 bug 或高风险专项验证需求时，优先扩展对应 system/Agent eval；需要长期保留的开发期
   专项检查进入独立 incubating 目录。
5. 真实外部连通性进入正式 system eval；模型决策和回答质量进入 Agent eval。
6. 新增核心测试必须说明 invariant ID、稳定行为变化，以及现有核心测试为何无法发现严重回归。
7. 无法给出上述理由时，任务报告必须写：

```text
Core invariant: unchanged.
Tests: not added because this is a node-level or implementation-only change.
```

禁止为 wrapper、配置字段、自然语言文案、prompt、description、覆盖率或假设性边缘场景新增测试。

## 10. 性能与稳定性目标

- 核心套件全部 mock/local/offline。
- 不访问网络、真实 Provider、外部进程或真实服务。
- 使用 in-memory 或本地受控 store。
- 正常开发机器上的目标运行时间不超过 60 秒。
- 新增节点、Tool、Provider 或 Agent Task 后，核心测试数量和预期结果默认保持不变。
- 核心套件失败必须能指向一个已登记框架不变量，而不是某个业务节点文案或供应商细节。

## 11. 权威文档同步范围

实施时同步：

- `AGENTS.md`：测试路由和后续 Codex 硬规则；
- `tests/README.md`：核心不变量、目录、准入、命令和汇报格式；
- `evals/README.md`：正式 system eval、incubating 和 Agent eval 边界；
- `README.md`：默认核心检查命令；
- `.codex/skills/assistant-agent-development-testing/SKILL.md`：默认不新增测试的决策 workflow；
- 引用旧测试目录或旧默认命令的当前架构文档。

历史计划和规格不作为当前权威，不批量改写。

## 12. 验收标准

- `pyproject.toml` 默认只收集 `tests/core`。
- `tests/` 中永久 Python 测试只位于 core；临时测试只位于 `tests/tdd/<feature>/`，根目录无测试。
- 显式运行 TDD feature 时强制 mock/offline，裸 pytest 不收集 TDD。
- 每个核心用例都关联有效 invariant ID。
- 核心策略检查能够阻止未知 ID、旁路测试文件和自然语言文案断言。
- 核心测试不导入具体业务节点或 Agent Task。
- 裸 pytest 在 mock 模式下通过，目标耗时不超过 60 秒。
- 每个保留的非核心专项检查位于独立 incubating 目录并声明删除条件。
- `AGENTS.md`、测试 skill、tests/evals 权威和 README 对默认边界描述一致。
- 本次迁移不调用真实 Provider，不覆盖或回滚无关工作区改动。
