# Mem0 Langfuse 记忆可视化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本机 Langfuse 的 Session/Trace 页面中展示每个 completed turn 经 Mem0 提炼后的具体长期记忆 change set，同时保持默认 canonical trace 不含记忆正文。

**Architecture:** Mem0 仍是唯一记忆事实源，`Mem0Client` 只保留原生 `POST /memories` 返回的 `id`、`memory` 与 `event`。运行时把数量、事件类型和 memory ID 写入 prompt-safe canonical event；只有显式启用本机记忆内容观测时，正文才进入有界进程内 overlay。由于 turn summary 早于后台 ingestion 完成，OTel observer 在原 trace 已导出后追加一个挂在稳定 `agent.runtime` root 下的 `memory.turn_ingestion` span；Langfuse 继续使用已有 `langfuse.session.id` 聚合一个 session 的全部 turn。

**Tech Stack:** Python 3.11、Pydantic、Mem0 OSS 2.0.11 REST、OpenTelemetry OTLP、Langfuse、pytest。

## Global Constraints

- 不调用真实 Provider，不修改 Mem0 的提取、合并、更新或持久化算法。
- Mem0 原生响应中的未知字段不进入运行时契约；只消费 `id`、`memory`、`event`。
- canonical trace、JSONL、公开 trace query 和普通控制台不得保存长期记忆正文。
- 记忆正文投影必须同时满足显式开关与 loopback OTLP endpoint；观测失败保持 fail-open。
- 不新增依赖，不新增 Memory CRUD/control-plane，不改变 session 冻结召回语义。
- `tests/tdd/mem0-langfuse-visualization/` 只作显式 mock/offline RED/GREEN，不进入默认 pytest。

---

### Task 1: 保留 Mem0 原生 change set

**Files:**
- Modify: `src/assistant_agent/memory/mem0/models.py`
- Modify: `src/assistant_agent/memory/mem0/client.py`
- Create: `tests/tdd/mem0-langfuse-visualization/test_mem0_change_set.py`

**Interfaces:**
- Consumes: Mem0 `POST /memories` 的 `{"results": [{"id": str, "memory": str, "event": "ADD" | "UPDATE" | "DELETE"}]}`。
- Produces: `Mem0MemoryChange` 与 `Mem0IngestionResult.changes`；现有 `memory_ids` 保持兼容。

- [x] **Step 1: 写失败测试**

  构造 dependency-free transport，依次返回 ADD、UPDATE、DELETE 与 malformed item；断言 `ingest_completed_turn()` 仅保留合法 change、规范化 event 为大写、保留最终 memory text，并从 change 派生现有 `memory_ids`。

- [x] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/mem0-langfuse-visualization/test_mem0_change_set.py`

  Expected: FAIL，因为 `Mem0MemoryChange` / `changes` 尚不存在。

- [x] **Step 3: 最小实现**

  新增结构化 model，并在 client 边界解析：缺少 ID 或不支持的 event 时跳过；memory 可为空以兼容 DELETE；`memory_ids` 继续包含所有合法 change 的 ID。

- [x] **Step 4: 运行 GREEN**

  Run: 与 Step 2 相同。

  Expected: PASS。

### Task 2: 分离 prompt-safe 事件与本地记忆正文 overlay

**Files:**
- Create: `src/assistant_agent/memory/trace_content.py`
- Modify: `src/assistant_agent/memory/observability.py`
- Modify: `src/assistant_agent/memory/service.py`
- Modify: `src/assistant_agent/observability/trace_content_policy.py`
- Create: `tests/tdd/mem0-langfuse-visualization/test_memory_trace_content.py`

**Interfaces:**
- Consumes: `Mem0IngestionResult.changes` 与当前 `AgentState` 的 trace/run/user/session identity。
- Produces: `MemoryIngestionTraceContent`、有界 `InMemoryMemoryTraceContentStore`、`local_memory_trace_content_enabled()`，以及不含正文的 `memory.ingestion.finished` canonical event。

- [x] **Step 1: 写失败测试**

  断言默认关闭时 canonical event 只含 `memory_count`、`change_counts`、`memory_ids`，递归检查不存在 memory text；显式开关打开时同一 trace/run 可从 overlay 取得 change text；超过容量时最旧记录被淘汰。

- [x] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/mem0-langfuse-visualization/test_memory_trace_content.py`

  Expected: FAIL，因为 overlay 与策略函数尚不存在。

- [x] **Step 3: 最小实现**

  用带锁 `OrderedDict` 建立最多 256 条的进程内 store；`record_ingestion_finished()` 在显式开关启用时先写 overlay，再 append canonical event。canonical event 只记录结构化数量、event 类型计数和 ID。

- [x] **Step 4: 运行 GREEN**

  Run: 与 Step 2 相同。

  Expected: PASS。

### Task 3: 把后台完成事件追加到已经导出的 Langfuse trace

**Files:**
- Modify: `src/assistant_agent/observability/otel_mapping.py`
- Modify: `src/assistant_agent/observability/otel_exporter.py`
- Create: `tests/tdd/mem0-langfuse-visualization/test_memory_otel_projection.py`

**Interfaces:**
- Consumes: 已导出的 runtime trace、晚到的 `memory.ingestion.finished` event、可选 `MemoryIngestionTraceContent`。
- Produces: `build_late_text_otel_span_spec()`；其 span 使用原 trace ID、稳定 `agent.runtime` parent span ID、`memory.turn_ingestion` 名称和 Langfuse session/user attributes。

- [x] **Step 1: 写失败测试**

  使用真实 `TextOtelTraceObserver` 与内存 fake exporter：先发送 `assistant.turn.summary` 触发主 trace export，再发送后台 memory finished event；断言出现第二个单 span batch、parent 指向原 root、session ID 保持一致。无内容权限时 output 只有计数/ID；有权限且 endpoint 为 loopback 时 output JSON 含 `ADD/UPDATE/DELETE` 结构和 memory text。

- [x] **Step 2: 运行 RED**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/mem0-langfuse-visualization/test_memory_otel_projection.py`

  Expected: FAIL，因为 observer 当前忽略已导出 run 的所有晚到事件。

- [x] **Step 3: 最小实现**

  给 mapping 增加只生成 operation span 的 late-event 入口，不重复导出 root；给 exporter config 增加 `include_memory_content`，它只在显式开关和 loopback endpoint 同时成立时为真；observer 对已导出 run 的 `memory.ingestion.finished` 单独导出 late span，其余晚到事件继续忽略。

- [x] **Step 4: 运行 GREEN**

  Run: 与 Step 2 相同。

  Expected: PASS。

### Task 4: 操作入口、文档与最小回归验证

**Files:**
- Modify: `scripts/run_server.py`
- Modify: `.env.example`
- Modify: `docs/memory-service-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: `MULTIMODAL_AGENT_LOCAL_MEMORY_TRACE_CONTENT`。
- Produces: `--allow-local-memory-trace-content` 本机显式启动开关和 PyCharm/Langfuse 查看说明。

- [x] **Step 1: 增加 operator 入口**

  `run_server.py` 增加 `--allow-local-memory-trace-content`，只设置上述环境变量；不自动开启 real Provider 或 OTLP。

- [x] **Step 2: 同步权威文档**

  写清 per-turn 异步 ingestion、Langfuse Session 聚合、late span、正文权限、Mem0 history 钻取和无 change 的显示语义。

- [x] **Step 3: 运行 feature 全集**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/mem0-langfuse-visualization`

  Expected: PASS，且没有网络或真实 Provider 调用。

- [x] **Step 4: 运行相关既有核心回归**

  Run: `MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/integration/test_memory_lifecycle.py tests/core/contract/test_observability_contract.py`

  Expected: PASS；证明后台生命周期与通用 OTel 映射未回归，但不把本功能临时测试晋升 core。

- [x] **Step 5: 检查变更范围**

  Run: `git diff --check && git status --short`

  Expected: 无 whitespace error；只包含本任务文件和用户原有未跟踪文件。
