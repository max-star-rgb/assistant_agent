# 核心 pytest 测试体系

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | pytest 目录归属、核心准入、验证范围与任务汇报的当前权威 |
| Owns | core invariant、`tests/core`、临时 `tests/tdd`、incubating 边界、最小测试选择与汇报格式 |
| Does not own | system eval/Release Review 运行协议、具体 feature 行为、真实 Provider 验证、产品架构 |
| 源码与 schema 入口 | `pyproject.toml`、`tests/core/INVARIANTS.md`、`tests/core/`、`tests/tdd/` |
| 验证入口 | `docs/authority.toml` 中 `test-policy.verification` |
| 相邻 authority | Eval 分层见 [`../evals/README.md`](../evals/README.md)；Codex 测试 workflow 见 [`../.codex/skills/assistant-agent-development-testing/SKILL.md`](../.codex/skills/assistant-agent-development-testing/SKILL.md) |

本文件是 pytest 目录归属、核心准入、验证范围和任务汇报的唯一事实权威。

**默认决定：不新增永久 pytest。** 永久、默认 pytest 只有 `tests/core/**`。只有已登记的 core
invariant 发生变化，或真实框架 bug 证明现有核心安全网存在缺口时，才修改已有 core 测试；没有
invariant ID 时禁止修改 `tests/core`。

具体 node、builtin Tool、Provider、Agent Task、配置、文案、prompt、description、console、wrapper
和覆盖率请求都不得进入 core。功能实现需要 TDD 时使用可手动删除的临时区；有风险证据才保留独立的
incubating 专项检查。

## 三个位置

```text
tests/
  core/                         # 永久核心安全网；裸 pytest 默认只收集这里
    INVARIANTS.md               # 核心不变量 ID、结构化契约和负责文件
    unit/                       # 窄纯逻辑核心契约
    contract/                   # 稳定协议与治理契约
    integration/                # 核心模块生命周期与协作
  tdd/
    <feature>/                  # 功能实现期间的临时 RED/GREEN pytest

evals/system/incubating/
  <feature>/
    README.md
    checks_*.py                 # 有风险证据才保留的显式专项检查
```

### `tests/core/`

`tests/core` 只保护与具体业务能力无关、预期长期稳定的框架不变量。每个 pytest item 都必须标记：

```python
@pytest.mark.core_invariant("TOOL-001")
def test_probe_tool_runs_through_governed_chain() -> None:
    ...
```

ID 必须先登记在 [`tests/core/INVARIANTS.md`](core/INVARIANTS.md)，测试文件也必须与登记的负责文件
一致。相同 invariant 优先扩展已有文件，不为一次功能或单个 bug 新建永久测试文件。

核心套件只能使用通用 Probe Tool、scripted adapter、in-memory/local store 和无语义 sentinel，必须
mock/local/offline，不读取真实 `.env`，不访问网络、真实 Provider、外部服务或付费 API。正常开发机器
目标运行时间小于 60 秒。新增 node、Tool、Provider 或 Task 默认不改变核心测试数量和结果。

### `tests/tdd/<feature>/`

这里仅保存功能实现期间的 RED/GREEN 临时 pytest：

- 每个功能必须使用自己的 `<feature>` 子目录，不得把测试放在 `tests/tdd/` 根目录；
- 只能显式运行，并由 `tests/tdd/conftest.py` 强制 mock/offline；
- 不需要 invariant ID，不进入默认收集，也不会自动晋升为 core；
- Codex 不得擅自删除；功能完成后用户可以手动删除整个 feature 目录，只有用户明确要求时 Codex
  才可代删。

临时 TDD 测试存在多久或有多少用例，都不构成晋升 core 的理由。

### `evals/system/incubating/<feature>/`

这里保存有明确风险证据、但不属于核心框架的 node、Provider adapter 或实现专项检查。每个 feature
目录必须自包含，使用 `checks_*.py` 和 README 说明 scope、mode、命令、副作用门禁、删除条件与晋升
路径。它们只能显式运行，不属于默认 pytest 或发布门禁。

incubating 不是正式真实 system eval。`checks_*.py` 必须保持 offline；真实 Tool、Context、Memory
或 Provider 连通性必须使用 `evals/system` 的正式 runner、real mode、完整配置和 operator 显式确认。
当正式 system eval、Release Review Experiment 或生产证据已稳定覆盖对应事实后，incubating feature
可以整目录删除，不应修改 core。

## Core 准入决策

按以下顺序决定，不得从“这次改了代码”直接推导出“需要永久测试”：

1. 本次变更是否改变 [`tests/core/INVARIANTS.md`](core/INVARIANTS.md) 中已登记的稳定、结构化框架
   契约？
2. 如果没有 invariant ID，禁止修改 `tests/core`。
3. 如果是具体 node、builtin Tool、Provider、Task、配置或实现细节，永久测试默认不新增。功能实现
   需要 RED/GREEN 时只放入 `tests/tdd/<feature>/`。
4. 如果有真实 bug，先证明它是框架 bug，并说明现有 core 安全网为何漏检；明确关联 invariant ID 后，
   才扩展该 invariant 已有的 core 测试。
5. 如果只是 node/provider 专项风险，且有持续观察价值，放入独立
   `evals/system/incubating/<feature>/`；待发布模型的工具决策、参数语义和回答质量放入
   `evals/release_review`。
6. 新增 core invariant 属于明确的框架契约决策：先登记 ID 和负责文件，再添加最小测试。覆盖率、
   文件变动或评审要求本身不是准入证据。

禁止为完整自然语言文案、prompt、Tool description、console 输出、简单 wrapper、配置常量、第三方
框架自身行为、私有实现调用次数或假设性边缘场景增加永久测试。

## 断言边界

core 断言必须指向稳定、可观察的结构化行为。

允许：

- 协议 token、invariant ID、枚举、状态和终态；
- schema 字段、结构化 result、error code 和恢复动作；
- 事件因果关系、身份隔离、持久化结果和可观察副作用；
- `*-sentinel` 等无语义透传值。

禁止：

- 完整自然语言回复、整段 prompt、description、渲染标签或控制台提示语；
- 用配置常量、序列化字段顺序或全文字符串包含证明行为；
- 导入具体 builtin Tool、具体 Provider 实现、Agent Task、grader 或 feature implementation；
- 以私有方法调用次数或 mock 调用次数作为主要正确性证据。

JSON 字符串先解析后断言结构化字段。敏感信息检查递归检查 key，不以序列化文本搜索作为主要证据。

## 验证命令

默认核心安全网；裸 pytest 只收集 `tests/core`：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

定向核心 invariant：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_tool_contract.py
```

临时 TDD feature：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/<feature>
```

incubating feature：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  evals/system/incubating/<feature>/checks_*.py
```

普通 feature 只运行对应 TDD/incubating 的最小显式集合。只有发布前、共享核心基础设施或已登记
invariant 发生变化、用户明确要求，或者定向验证暴露出无法界定的核心影响时，才运行裸 pytest。
不得仅因任务结束或 skill 被触发而机械运行裸 pytest。

真实 Provider 永不进入 pytest。正式 system eval 和 Release Review 的命令、安全门禁及结果权威见
[`evals/README.md`](../evals/README.md)。

## 任务汇报

每次开发任务结束时同时汇报 `Core invariant:` 和 `Tests:`，并列出实际执行命令。

未改变核心不变量：

```text
Core invariant: unchanged.
Tests: not added because this is a node-level or implementation-only change.
```

临时 TDD：

```text
Core invariant: unchanged.
Tests: added/updated tests/tdd/<feature> for temporary RED/GREEN; user may delete the directory manually.
```

修改核心安全网：

```text
Core invariant: TOOL-001 changed because <stable framework behavior>.
Tests: updated <existing core test>.
```

不影响可观察行为时也可写：

```text
Core invariant: unchanged.
Tests: not added because the change does not affect a stable framework contract.
```

若调用了真实 Provider，必须在 pytest 汇报之外另列 system eval/Release Review 的范围、门禁和结果。
