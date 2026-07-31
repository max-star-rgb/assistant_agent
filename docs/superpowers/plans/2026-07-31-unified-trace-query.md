# 统一 Trace 查询与本地持久化回退实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复非空 trace HTTP 查询 500，并让 Server 重启后的历史 trace 能从本地 JSONL 回退读取，同时保持 Langfuse 为主要可视化入口、本地事实链为离线兜底。

**Architecture:** `TraceEvent` 继续是唯一运行事实模型。`CompositeTraceStore` 仍向内存 primary、本地 JSONL 和可选 observer 扇出写入，但只把显式登记的 JSONL store 作为 read fallback；当前进程优先读取内存，未命中时读取持久化 JSONL。`TraceQueryService` 只消费统一的 `trace_debug_summary`，可选摘要字段缺失时使用模型默认值，避免投影漂移变成 HTTP 500。

**Tech Stack:** Python 3.12、Pydantic、FastAPI、pytest、本地 JSONL、现有 OTel/Langfuse observer。

## Global Constraints

- 不把 Langfuse 或网络服务放到 Runtime 响应关键路径。
- 不删除 `TraceEvent`、内存 store、JSONL、Gateway lifecycle 或 delivery audit。
- Langfuse 继续承担跨进程可视化和主要人工查询；JSONL 承担离线、导出失败与本机恢复兜底。
- read fallback 必须显式配置，不能把只写 observer 当成可查询数据源。
- 默认测试保持 mock/local/offline，不读取真实 `.env`、不访问 Langfuse。
- 只修改与 `OBS-001` 相关的现有 core 测试文件和登记说明。

---

### Task 1: 修复统一 trace summary 契约

**Files:**
- Modify: `src/assistant_agent/observability/trace_store.py`
- Modify: `src/assistant_agent/observability/trace_query.py`
- Modify: `tests/core/contract/test_observability_contract.py`

**Interfaces:**
- Consumes: `trace_debug_summary(events: list[TraceEvent]) -> dict[str, Any]`
- Produces: 非空 summary 始终包含 `budget_exceeded` 和 `retry_count`；`TraceQueryService.trace_summary()` / `run_summary()` 对可选诊断字段缺失保持默认值。

- [x] **Step 1: 写失败测试**

在现有 `OBS-001` 文件中构造一个非空 `InMemoryTraceStore`，调用 `TraceQueryService.trace_summary()`，断言结果可序列化且 `budget_exceeded is False`。

- [x] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/core/contract/test_observability_contract.py -k non_empty_trace
```

预期：失败于当前 `KeyError: 'budget_exceeded'`。

- [x] **Step 3: 最小实现**

让 `trace_debug_summary()` 的空/非空分支输出同一组顶层键；从结构化 context budget 或明确属性推导 `budget_exceeded`，没有证据时为 `False`。`TraceQueryService` 使用 `.get()` 和 Pydantic 默认语义读取可选诊断字段。

- [x] **Step 4: 运行定向测试确认 GREEN**

执行 Step 2 相同命令，预期通过。

### Task 2: 增加显式 JSONL read fallback

**Files:**
- Modify: `src/assistant_agent/observability/trace_store.py`
- Modify: `src/assistant_agent/observability/trace_persistence.py`
- Modify: `tests/core/contract/test_observability_contract.py`
- Modify: `tests/core/INVARIANTS.md`

**Interfaces:**
- Produces: `CompositeTraceStore(..., read_fallbacks: Iterable[TraceStore] = ())`
- Behavior: `list_by_run`、`list_by_trace`、`list_by_user` 和 `node_path` 优先 primary；primary 未命中时按配置顺序查询 read fallback。

- [x] **Step 1: 写失败测试**

使用临时 JSONL 路径创建第一套 server trace store、写入并关闭；重建 store 后按同一 trace ID 查询，断言能得到原始关联事件。

- [x] **Step 2: 运行测试确认 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/core/contract/test_observability_contract.py -k persisted_trace
```

预期：新 store 的内存 primary 为空，查询返回空列表。

- [x] **Step 3: 最小实现**

为 `CompositeTraceStore` 增加显式 read fallback；读取异常遵循现有 `continue_on_error` 并写入安全 dispatch error。`create_server_trace_store()` 只把 `BufferedJsonlTraceStore` 登记为 read fallback，不登记 OTel/Langfuse hook observer。

- [x] **Step 4: 运行定向测试确认 GREEN**

执行 Step 2 相同命令，预期通过；更新 `OBS-001` 描述，纳入持久化 read-through。

### Task 3: 文档与验证

**Files:**
- Modify: `docs/observability-harness.md`

**Interfaces:**
- Documents: Langfuse 主可视化、内存实时读取、JSONL 跨重启回退和统一 summary builder 的职责。

- [x] **Step 1: 同步架构文档**

删除“Composite reads primary-only”的旧描述，明确 Server 查询顺序和 Langfuse 不在关键路径。

- [x] **Step 2: 运行 OBS-001 定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
tests/core/contract/test_observability_contract.py
```

- [x] **Step 3: 运行默认核心安全网**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

- [x] **Step 4: 静态检查**

```bash
git diff --check
```

确认没有真实 Provider/Langfuse 调用；报告 `OBS-001` 变更与实际命令。
