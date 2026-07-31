---
name: assistant-agent-development-testing
description: Use when adding or updating pytest tests, deciding the verification scope for a code change, or diagnosing deterministic pytest failures in the assistant_agent repository.
---

# Assistant Agent 测试决策

本 skill 只路由测试决策，不指导功能实现。开始前完整读取 `tests/README.md` 和
`tests/core/INVARIANTS.md`；它们是规则与核心契约的事实权威。

## 必须执行

1. 默认不新增永久测试。
2. 先给出 core invariant ID。没有 ID 时禁止修改 `tests/core`；真实框架 bug 只有在证明现有安全网
   缺口并关联 ID 后，才可扩展该 invariant 的已有 core 文件。
3. feature 实现需要 RED/GREEN 时只使用 `tests/tdd/*/` 下独立的 feature 子目录，显式
   mock/offline 运行。它不进入
   默认收集、不自动晋升 core；Codex 不得擅自删除，用户可手动删除整个 feature 目录。
4. node 或 Provider 专项只有存在风险证据和持续观察价值时，才进入独立
   `evals/system/incubating/<feature>/`；模型行为进入 `evals/agent`，真实 Provider 永不进入 pytest。
5. 不为文案、prompt、description、console、wrapper、配置常量或覆盖率添加测试。
6. 运行能证明本次变更的最小显式集合。普通 feature 只运行对应 TDD/incubating；只有发布前、共享
   核心基础设施或已登记 invariant 变化、用户明确要求，或定向失败显示影响无法界定时，才运行裸
   `pytest`。
7. 按权威格式同时汇报 invariant 决策、测试决策和实际命令。

## 汇报

```text
Core invariant: unchanged.
Tests: not added because this is a node-level or implementation-only change.
```

或：

```text
Core invariant: TOOL-001 changed because <stable framework behavior>.
Tests: updated <existing core test>.
```

临时 TDD 还要说明对应 `tests/tdd/*/` feature 可由用户手动整目录删除；不得替用户自动删除或把它自动
晋升为 core。若本 skill 与权威冲突，以 `tests/README.md` 为准并修正本入口。
