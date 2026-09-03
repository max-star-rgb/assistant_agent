# Durable Task 状态机架构

最后更新：2026-09-02

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 非对话型 durable task 的计划、持久化状态机、lease、恢复与 worker 权威 |
| Owns | `TaskPlan/TaskStep`、task/checkpoint/wait schema、状态转换、Store、lease、resume、worker 与 failure atomicity |
| Does not own | Assistant Graph、Tool schema/HITL、媒体主动投递 wire、Provider 调用和长期 Memory |
| 源码与 schema 入口 | `src/assistant_agent/automation/durable_tasks/` |
| 验证入口 | `docs/authority.toml` 中 `durable-tasks.verification`；核心不变量 `DUR-001` |
| 相邻 authority | Tool 接入见 [`tool-calling-architecture.md`](tool-calling-architecture.md)；测试策略见 [`../tests/README.md`](../tests/README.md) |

## 当前边界

Durable task 是对话 run 之外的窄业务状态机，不是第二套 Agent Runtime。`DurableTaskRequest`、`TaskPlan`、`TaskStep`、task record、
plan version、step run、artifact、wait、notification 和 checkpoint schema 统一位于
`automation/durable_tasks/models.py`。计划校验、状态转换、identity 隔离、lease 与恢复由
`DurableTaskService` 所有；内存与 SQLite Store 实现同一 `TaskStore` 协议。

worker 每次领取一个受保护的 task lease，并通过 `DurableTaskRequest` 把 identity 与当前持久化 snapshot 交给对应
窄 runtime；runtime 通过结构化
`TaskQuantumResult(checkpoint, binding)` 提交成功、失败、等待或终态，不创建或返回对话 Agent state。
service/worker 在同一 Store 上重建后，已登记 schedule 必须恢复且只执行一次；版本、lease token、wait ID 和
幂等 key 共同阻止旧 worker 或重复 wake 覆盖新状态。durable runtime 使用受信 allowlist、side-effect 记录和窄业务
adapter；对话 Agent 中的 Tool 才走标准 `BaseTool -> ToolNode` 与原生 HITL。durable 状态机不拥有通用 Tool executor。

`hotel_price_watch.py` 是当前保留的具体 workflow；它只能使用显式允许的 read Tool，并通过同一 service、Store
和 worker 契约推进。媒体 proactive outbox 与视觉提醒是相邻交付机制，不属于本状态机，也不能用 socket 写成功
替代 durable checkpoint。

## 物理归属

`DurableTaskRequest` 与 `TaskPlan/TaskStep` 均由 `automation/durable_tasks/models.py` 所有；通用 `runtime/`
不再保留对话式 request/response/citation 兼容 DTO。Tool adapter 只通过该 service
创建或查询任务；它不得携带状态机、lease、checkpoint 或状态转换实现。Runtime 和 Tool 包不得重新定义这些契约。

## 验证

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q \
  tests/core/integration/test_durable_lifecycle.py
```
