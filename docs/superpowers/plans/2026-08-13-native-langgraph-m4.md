# 原生 LangGraph M4：Production Cutover、持久化与 Legacy 收缩实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 production Durable Workflow 全量切到持久化 `DurableWorkflowGraph`，安全排空并迁移 legacy rows，随后删除自研 scheduler、lease/CAS/ready-node、影子 Workflow OTel 与无人消费的 legacy definition/config，同时保持真实产品协议、领域事实和独立 `automation.durable_tasks` 能力不变。

**Architecture:** 先建立进程级 official async SQLite saver、严格产品仓库和 `WorkflowGraphHost`，让所有新提交显式写入 `execution_engine="langgraph_v3"` 并由 compiled graph 执行；旧 `legacy_scheduler_v2` 只允许 drain，不再接收新提交。只有 production cutover、跨进程恢复、legacy 非终态归零和终态 rows 迁移共同通过 Gate 0 后，才按消费者从外到内停止 worker、删除 runtime/executor、删除 scheduler store/schema，最后移除影子观测与 legacy config；LangGraph checkpointer 保存执行位置，业务 SQLite 只保存 owner、submission identity、严格产品快照/事件、artifact、审计和幂等事实。

**Tech Stack:** Python 3.12、LangGraph 1.2.4 Graph API、langgraph-checkpoint 4.1.1、`langgraph-checkpoint-sqlite==3.1.0`、Pydantic v2、asyncio、SQLite、FastAPI、LangSmith、pytest。

## Global Constraints

- 本计划是 M4 production cutover 与双轨收缩，不提前实施 M5 的普通对话全面收敛，也不删除 Langfuse 的非 Workflow 能力。
- Gate 0 是所有 destructive cleanup 的硬前置：official persistent saver、`WorkflowGraphHost` production cutover、新提交 `langgraph_v3`、跨进程 recovery、legacy drain、终态旧 rows 迁移任一未通过时，Task 3–8 均不得开始。
- official saver 缺依赖、数据库不可创建、`setup()` 失败或 production 配置仍为 memory/none 时必须启动失败；禁止静默回退 `InMemorySaver`，后者只允许在 mock/offline 单测中显式注入。
- `langgraph-checkpoint-sqlite==3.1.0` 的安装与 lock 更新需用户明确授权；未授权时 Task 0 保持 RED，不能以手写 saver 或跳过 persistent test 绕过 Gate 0。
- 新提交只能写 `execution_engine="langgraph_v3"`；`legacy_scheduler_v2` 只读、只 drain、禁止新建。engine 不由请求文本、plan kind、definition 名称或 allowlist 猜测。
- `WorkflowGraphHost` 只拥有 graph task、订阅、interrupt/resume/cancel/recovery 和产品投影；不计算 ready node、不 claim、不续租、不做 revision CAS。
- Workflow API 只返回严格 `WorkflowProductRecord`、`WorkflowProductEvent` 与 artifact 内容；不得返回 raw `WorkflowBundle`、plan/work item、checkpoint、namespace、task、native interrupt ID、Provider raw response 或 Tool raw body。
- 真实消费者边界：保留 Agent-Service/媒体的 `workflow://<workflow_id>` output ref、身份隔离、`GET /workflows/{id}`、events、result、input、cancel 路径和结果交付语义；同步更新仓库内真实客户端 `scripts/media_simulator.py`。内部 raw plan/event schema、`resume_token`、legacy worker API 和未被真实客户端使用的兼容对象允许 breaking cleanup。
- Tool 调用继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`；artifact ownership、业务幂等、审计与 commit barrier 不进入 checkpointer 替代。
- `src/assistant_agent/automation/durable_tasks/**`、其配置、worker、store、API、测试和 `DUR-001` 永远不在本计划删除或迁移范围；任何 broad source/AST gate 必须显式排除该目录。
- LangSmith 是 Workflow graph 的原生 trace/eval 目标。Task 7 只删除为 legacy Workflow scheduler 重建树的 `observed_store.py`、`workflow_otel.py`、`workflow_trace.py`；不删除通用 canonical trace、runtime audit、Agent-Service delivery audit 或 Langfuse 的非 Workflow 路径。
- Core invariant 初始决策：`LOOP-001`、`IDENT-001`、`OBS-001` 需要在 Gate 0 后回补最小长期结构化契约；`DUR-001` 不变且必须继续通过。具体 Workflow definition、DB migration 细节与删除实现继续放在可手动删除的 `tests/tdd/native-langgraph-m4/`。
- 全部 pytest 默认 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`、local/offline；真实 Provider 与远端 LangSmith 只在正式 Release Review/operator gate 下运行，不能进入 pytest。
- 每个删除任务先证明最后真实 consumer 已切走，再执行 source gate、AST import gate 和文件删除 gate；“零调用 getter”本身不构成提前删除理由。

## Gate 0：删除前不可分割的上线门

Gate 0 由 Task 0–2 共同完成，验收证据必须来自同一候选 checkout：

1. production 使用 official async SQLite saver，跨关闭/重建进程级 owner 后，同一 `thread_id` 能恢复非终态 graph；
2. `WorkflowGraphHost` 是 production Deep Research 的唯一 submit/resume/cancel/recovery owner；
3. 新提交业务 row 显式为 `langgraph_v3`，且从提交到终态对 `claim_ready_work_item`、`renew_work_item_lease`、`WorkflowRuntime.run_claim`、`AgentGraphRuntime.run_work_item` 调用数均为零；
4. cutover 时先禁止 legacy 新提交，再让 legacy worker 只 drain 已存在的 `legacy_scheduler_v2` 非终态 rows；
5. `legacy_nonterminal_count() == 0`，全部 legacy terminal rows 已幂等迁入严格产品表，迁移计数、owner、artifact refs、terminal status 与 event cursor 守恒；
6. API 与 `media_simulator` 只消费严格产品 DTO，Agent-Service/媒体 `workflow://` output ref 和 final artifact 交付兼容；
7. production restart 扫描并恢复 `langgraph_v3` 非终态 thread，不重复 publish/tool 副作用；
8. Gate 0 artifact 明确记录 DB backup 路径、row counts、迁移校验摘要和 rollback 条件；Task 3 前由 operator 确认，不能由测试自动删除旧表或备份。

---

### Task 0: Official persistent saver、进程级 owner 与 compiled graph 生命周期

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/assistant_agent/config/__init__.py`
- Replace: `src/assistant_agent/runtime/checkpointer.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Modify: `src/assistant_agent/runtime/runtime_host.py`
- Modify: `src/assistant_agent/workflows/durable_graph.py`
- Modify: `src/assistant_agent/workflows/durable_graph_app.py`
- Create: `tests/tdd/native-langgraph-m4/test_persistent_workflow_host.py`
- Create: `tests/tdd/native-langgraph-m4/test_workflow_checkpointer_lifecycle.py`

**Interfaces:**
- Consumes: existing `build_durable_workflow_graph(...)`、`DurableWorkflowGraphApp.arun()/aresume()/aget_state()`、`WorkflowGraphRuntimeContext`、`WorkflowGraphExecutionIdentity`。
- Produces: `AsyncCheckpointerOwner.astart() -> None`、`.saver -> BaseCheckpointSaver`、`.aclose() -> None`；`open_async_checkpointer(*, backend: Literal["sqlite"], path: Path) -> AsyncCheckpointerOwner`；由同一 saver 编译的 `AssistantTurnGraphApp` 与 `DurableWorkflowGraphApp`。
- Ownership: `AssistantRuntimeApp`/FastAPI lifespan 创建唯一 saver owner，compiled assistant graph 与 workflow graph 共用该 owner；child subgraph 继续 `checkpointer=None` 继承 namespace。
- Breaking cleanup boundary: 允许把 `create_checkpointer()` 的 production sync memory factory 改成显式 test-only helper；不改变普通对话外部 wire，也不修改 durable task saver/store。

- [ ] **Step 1: 写 persistent saver 与 host 生命周期 RED**

```python
@pytest.mark.asyncio
async def test_sqlite_workflow_host_recovers_after_owner_recreation(tmp_path):
    first_owner, first_app, identity, context, initial = await build_persistent_probe(
        tmp_path / "checkpoints.sqlite3"
    )
    waiting = await first_app.arun(initial, identity=identity, context=context)
    assert waiting.status == "interrupted"
    action_ref = waiting.interrupts[0].action_ref
    await first_owner.aclose()

    second_owner, second_app = await rebuild_persistent_probe(
        tmp_path / "checkpoints.sqlite3"
    )
    completed = await second_app.aresume(
        identity=identity.with_new_run_id("resume-run-sentinel"),
        context=context,
        resume=WorkflowResume(values_by_action_ref={action_ref: {"response": "sentinel"}}),
    )
    assert completed.status == "completed"
    await second_owner.aclose()


@pytest.mark.asyncio
async def test_sqlite_saver_setup_failure_is_not_replaced_with_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(AsyncSqliteSaver, "setup", _raise_sqlite_error)
    with pytest.raises(RuntimeError, match="persistent_checkpointer_unavailable"):
        await open_async_checkpointer(backend="sqlite", path=tmp_path / "bad.sqlite3")
```

- [ ] **Step 2: 运行 RED，确认缺 official dependency/owner/host**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_persistent_workflow_host.py \
  tests/tdd/native-langgraph-m4/test_workflow_checkpointer_lifecycle.py
```

Expected: FAIL，明确缺 `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`、`open_async_checkpointer` 或 async owner；不得通过 skip/xpass 隐藏。

- [ ] **Step 3: 经用户授权后锁定依赖并实现 async owner**

`pyproject.toml` 增加：

```toml
"langgraph-checkpoint-sqlite==3.1.0",
```

`checkpointer.py` 使用 official API 并强制 strict msgpack：

```python
class AsyncCheckpointerOwner:
    async def astart(self) -> None:
        self._context = AsyncSqliteSaver.from_conn_string(str(self.path))
        self._saver = await self._context.__aenter__()
        await self._saver.setup()

    @property
    def saver(self) -> BaseCheckpointSaver:
        if self._saver is None:
            raise RuntimeError("persistent_checkpointer_not_started")
        return self._saver

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
```

production 配置只接受 `langgraph_checkpointer_backend="sqlite"`；`memory` 仅由测试显式注入，不作为 production fallback。启动前设置 `LANGGRAPH_STRICT_MSGPACK=true`，checkpoint 仍只含严格 JSON-safe state/ref，不扩大 allowed modules。

- [ ] **Step 4: 让 assistant/workflow compiled graph 共用 saver owner**

`AssistantRuntimeApp.astart()` 先启动 owner，再把同一 `owner.saver` 注入顶层 assistant graph 和 workflow graph builder；两个 graph 的 child subgraph 都继续以 `checkpointer=None` 继承父 namespace。`AssistantRuntimeApp.aclose()` 先等待 graph 调用退出，再关闭 saver；不得把 saver connection 交给单个 graph app 关闭。Task 2 的 `WorkflowGraphHost` 复用此 owner，不另建 connection/event-loop thread。

- [ ] **Step 5: 运行 GREEN 与生命周期泄漏检查**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_persistent_workflow_host.py \
  tests/tdd/native-langgraph-m4/test_workflow_checkpointer_lifecycle.py \
  tests/tdd/native-langgraph-m3/test_workflow_interrupt_resume.py
```

Expected: PASS；第二个 host 使用同一 SQLite checkpoint 继续相同 thread，新 resume run ID 不同；关闭后无遗留 task/connection，setup failure fail closed。

- [ ] **Step 6: 运行 AST/source gate**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python - <<'PY'
import ast
from pathlib import Path
paths = [Path('src/assistant_agent/runtime/assistant_runtime_app.py'), Path('src/assistant_agent/runtime/checkpointer.py')]
for path in paths:
    tree = ast.parse(path.read_text())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    forbidden = names & {'DurableWorkflowWorker', 'WorkflowRuntime', 'ThreadPoolExecutor'}
    assert not forbidden, (path, forbidden)
PY
```

Expected: exit 0；host/saver owner 不依赖 legacy worker/runtime/thread pool。

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/assistant_agent/config/__init__.py \
  src/assistant_agent/runtime/checkpointer.py \
  src/assistant_agent/runtime/assistant_runtime_app.py \
  src/assistant_agent/runtime/runtime_host.py \
  src/assistant_agent/workflows/durable_graph.py \
  src/assistant_agent/workflows/durable_graph_app.py \
  tests/tdd/native-langgraph-m4/test_persistent_workflow_host.py \
  tests/tdd/native-langgraph-m4/test_workflow_checkpointer_lifecycle.py
git commit -m "feat(workflows): own persistent workflow graph lifecycle"
```

### Task 1: 严格产品仓库、旧 rows 清点迁移与 drain 控制面

**Files:**
- Create: `src/assistant_agent/workflows/product_models.py`
- Create: `src/assistant_agent/workflows/product_repository.py`
- Create: `src/assistant_agent/workflows/sqlite_product_repository.py`
- Create: `src/assistant_agent/workflows/legacy_migration.py`
- Modify: `src/assistant_agent/workflows/graph_projection.py`
- Create: `scripts/migrate_legacy_workflows.py`
- Modify: `scripts/README.md`
- Create: `tests/tdd/native-langgraph-m4/test_workflow_product_repository.py`
- Create: `tests/tdd/native-langgraph-m4/test_legacy_workflow_migration.py`

**Interfaces:**
- Produces: strict frozen `WorkflowProductRecord`、`WorkflowProductEvent`、`WorkflowSubmissionIdentity`；`WorkflowProductRepository.create_admission()/save_projection()/get_owned()/list_events()/append_event()/list_nonterminal_graph()/close()`。
- Produces: `LegacyWorkflowMigrator.inventory() -> LegacyInventory`、`.migrate_terminal_rows() -> MigrationReport`、`.legacy_nonterminal_count() -> int`；CLI `inspect|migrate-terminal|verify-drain`。
- Consumes: legacy `SQLiteWorkflowStore` only inside `legacy_migration.py`; Task 2 的 graph host 和 API 必须消费 `WorkflowProductRepository`，never `WorkflowBundle`。
- Real consumers: status/events/result/input/cancel routes and media simulator need owner、status、phase、progress、waiting action、artifact refs；这些字段进入严格 DTO。raw plan、work item attempt、lease、budget reservation、legacy event payload 无产品 consumer，明确不迁移。
- Breaking cleanup boundary: 保留旧 `durable_workflows`/`durable_workflow_events` 表只到 Gate 0 backup+drain 完成；迁移是幂等复制，不在本 Task drop 表。

- [ ] **Step 1: 写产品仓库与 migration RED**

```python
def test_product_repository_rejects_raw_execution_fields(tmp_path):
    repo = SQLiteWorkflowProductRepository(tmp_path / "products.sqlite3")
    payload = _product_record_dict()
    payload["plan"] = {"work_items": []}
    with pytest.raises(ValidationError):
        repo.create_admission(WorkflowProductRecord.model_validate(payload))


def test_terminal_migration_is_idempotent_and_preserves_product_facts(tmp_path):
    legacy = _legacy_db(tmp_path, terminal=3, nonterminal=2)
    products = SQLiteWorkflowProductRepository(tmp_path / "products.sqlite3")
    migrator = LegacyWorkflowMigrator(legacy=legacy, products=products)
    first = migrator.migrate_terminal_rows()
    second = migrator.migrate_terminal_rows()
    assert (first.inserted, first.already_present) == (3, 0)
    assert (second.inserted, second.already_present) == (0, 3)
    assert migrator.legacy_nonterminal_count() == 2
    assert _terminal_fact_digest(legacy) == _terminal_fact_digest(products)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_workflow_product_repository.py \
  tests/tdd/native-langgraph-m4/test_legacy_workflow_migration.py
```

Expected: FAIL，缺 strict repository/migrator。

- [ ] **Step 3: 定义严格 product schema 与 repository schema**

`WorkflowProductRecord` 精确包含：

```python
class WorkflowProductRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["workflow_product_record_v1"]
    workflow_id: str
    execution_engine: Literal["langgraph_v3"]
    workflow_type: Literal["deep_research"]
    user_id: str
    agent_id: str
    session_id: str
    ingress_run_id: str
    idempotency_key: str
    submission_digest: str
    objective: str
    deliverables: tuple[str, ...]
    status: WorkflowGraphStatus
    phase: WorkflowPhase
    revision: int
    remaining_budget: PersistedWorkflowBudget
    progress: WorkflowProductProgress
    waiting_actions: tuple[WorkflowWaitingAction, ...]
    result_artifact_refs: tuple[str, ...]
    terminal_reason_code: str | None
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None
```

SQLite 新表固定为 `workflow_products`、`workflow_product_events`；products 表只建立 owner/idempotency、status/update 查询索引，不建 claim/lease/ready 索引。`save_projection()` 以 `event_id` 幂等并只允许合法状态演进；这不是 scheduler revision CAS，不选择下一节点。

- [ ] **Step 4: 实现只读 legacy inventory、终态迁移与 rollback artifact**

CLI 行为：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/migrate_legacy_workflows.py \
  inspect --db .local/workflows/workflows.sqlite3 --output .data/workflow-m4/inventory.json
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/migrate_legacy_workflows.py \
  migrate-terminal --db .local/workflows/workflows.sqlite3 \
  --backup .data/workflow-m4/workflows-before-m4.sqlite3 \
  --report .data/workflow-m4/migration-report.json
```

`migrate-terminal` 先用 SQLite backup API 创建一致性备份，再迁移 completed/failed/cancelled rows；遇到非法 owner、重复 idempotency 不同 digest、非法 artifact ref 或 terminal mismatch 时整体 fail closed，不修改原表。报告只含计数、ID digest、status histogram、backup 路径，不含 objective、input、artifact 内容或 Provider payload。

- [ ] **Step 5: 运行 GREEN 与 schema 负向检查**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_workflow_product_repository.py \
  tests/tdd/native-langgraph-m4/test_legacy_workflow_migration.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/migrate_legacy_workflows.py \
  inspect --db /tmp/nonexistent-workflow-db --output /tmp/workflow-m4-inspect.json
```

Expected: pytest PASS；CLI 对不存在/非 Workflow DB fail closed 且不创建 production 文件。

- [ ] **Step 6: 运行 raw-field source gate**

```bash
! rg -n "WorkflowBundle|WorkflowWorkItem|lease_|checkpoint|namespace|native_interrupt" \
  src/assistant_agent/workflows/product_models.py \
  src/assistant_agent/workflows/product_repository.py \
  src/assistant_agent/workflows/sqlite_product_repository.py
```

Expected: 零命中。

- [ ] **Step 7: Commit**

```bash
git add src/assistant_agent/workflows/product_models.py \
  src/assistant_agent/workflows/product_repository.py \
  src/assistant_agent/workflows/sqlite_product_repository.py \
  src/assistant_agent/workflows/legacy_migration.py \
  src/assistant_agent/workflows/graph_projection.py \
  scripts/migrate_legacy_workflows.py scripts/README.md \
  tests/tdd/native-langgraph-m4/test_workflow_product_repository.py \
  tests/tdd/native-langgraph-m4/test_legacy_workflow_migration.py
git commit -m "feat(workflows): persist strict graph product records"
```

### Task 2: Production `graph_v3` cutover、严格 API 与 legacy drain 验收

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/assistant_runtime_app.py`
- Create: `src/assistant_agent/workflows/graph_host.py`
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/api/routes_workflows.py`
- Modify: `src/assistant_agent/api/models.py`
- Modify: `src/assistant_agent/runtime/server_startup_summary.py`
- Modify: `scripts/media_simulator.py`
- Modify: `scripts/README.md`
- Modify: `evals/release_review/cli.py`
- Create: `tests/tdd/native-langgraph-m4/test_production_workflow_cutover.py`
- Create: `tests/tdd/native-langgraph-m4/test_workflow_api_projection.py`
- Create: `tests/tdd/native-langgraph-m4/test_legacy_drain_gate.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_durable_lifecycle.py`
- Modify: `tests/core/contract/test_observability_contract.py`

**Interfaces:**
- Produces: `WorkflowGraphHost.astart()/asubmit()/aget()/alist_events()/aresume()/acancel()/arecover_nonterminal()/aclose()`；所有公共方法返回 Task 1 strict product DTO，内部 graph result 不越过 host。
- Replaces: `_start_deep_research_workflow(state, workflow_service=...)` with async `_start_deep_research_workflow(state, workflow_graph_host=...)` calling `await host.asubmit(...)`.
- API dependency becomes `get_workflow_graph_host() -> WorkflowGraphHost`; response models use concrete `WorkflowProductRecordResponse`/`WorkflowProductEventResponse` rather than `dict[str, Any]`。
- Input contract becomes `{action_ref: str, values: dict[str, str]}`；native interrupt ID 和旧 `resume_token` 不接受。cancel remains `POST /workflows/{id}/cancel` and becomes host-owned graph cancellation plus product projection。
- Real consumers: `scripts/media_simulator.py` reads `workflow.status`、`progress`、`waiting_actions[0].action_ref`、result content；Agent-Service keeps `workflow://` output ref unchanged。
- Breaking cleanup boundary: route paths、identity/query policy、result artifact response and protocol version stay stable；raw `plan` response member、raw legacy events、`waiting_input.resume_token` are intentionally removed because no protected external contract owns them。

- [ ] **Step 1: 写 production cutover、API 与 drain RED**

```python
@pytest.mark.asyncio
async def test_new_deep_research_submission_is_graph_v3_and_never_claimed(spies, app):
    response = await _submit_deep_research(app, "sentinel objective")
    record = await app.state.workflow_graph_host.aget(response.workflow_id, _identity())
    assert record.execution_engine == "langgraph_v3"
    assert spies.claim_ready_work_item.call_count == 0
    assert spies.renew_work_item_lease.call_count == 0
    assert spies.run_claim.call_count == 0
    assert spies.run_work_item.call_count == 0


def test_gate_zero_rejects_cleanup_while_legacy_nonterminal_rows_exist(migrator):
    assert migrator.legacy_nonterminal_count() == 1
    with pytest.raises(LegacyDrainIncomplete, match="legacy_nonterminal_rows=1"):
        assert_gate_zero_ready(migrator)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_production_workflow_cutover.py \
  tests/tdd/native-langgraph-m4/test_workflow_api_projection.py \
  tests/tdd/native-langgraph-m4/test_legacy_drain_gate.py
```

Expected: FAIL；production 仍走 `WorkflowService.submit()`、API 仍返回 Bundle/plan，legacy drain 无硬 gate。

- [ ] **Step 3: 切 production 新提交为 host-only graph_v3**

FastAPI lifespan 顺序固定为：

```text
start durable task worker（独立能力）
→ start shared AsyncCheckpointerOwner
→ start WorkflowGraphHost
→ recover langgraph_v3 nonterminal rows
→ serve
→ stop WorkflowGraphHost
→ close AsyncCheckpointerOwner
→ stop durable task worker（独立能力）
```

`AgentGraphRuntime` 不再创建 `WorkflowService`/`ObservedWorkflowStore` 作为 Deep Research submit owner；它接收已启动的 `workflow_graph_host`。async 主路径 await host；同步 `run_state()` 若收到 `assistant_mode=deep_research` 返回结构化 `workflow_async_entry_required`，不得重新引入 event-loop thread 或 legacy submit。

`WorkflowGraphHost.asubmit()` 精确执行：验证只允许 `deep_research`；用 `workflow_<32 hex>`、稳定 `workflow_thread_id` 和新 invocation run ID 建立 `langgraph_v3` admission；先 `repository.create_admission()`，再调用 `DurableWorkflowGraphApp.arun()`；stream custom facts 与 final state 分别经 `WorkflowGraphProjector.project_stream_part()`/`.project_snapshot()` 写 `save_projection()`。`aresume()` 从 repository 校验 owner 与 pending `action_ref`，用相同 thread/new run ID 调 graph app；`arecover_nonterminal()` 枚举 repository rows 并从 saver snapshot 恢复。每个 workflow 由 `_tasks: dict[str, asyncio.Task[None]]` 保证单 owner；`aclose()` 有界等待 active tasks，但不关闭共享 saver。

- [ ] **Step 4: 切 API 与 media simulator 到 strict projection**

`WorkflowResponse` 精确返回：

```python
class WorkflowResponse(BaseModel):
    protocol_version: str = PROTOCOL_VERSION
    workflow: WorkflowProductRecordResponse
    progress: WorkflowProductProgress
```

events 只返回 `WorkflowProductEvent`；result 从 record 的最后一个 artifact ref 经 owner-bound artifact store 读取；input 按 `action_ref` 调用 `host.aresume()`；cancel 调用 `host.acancel()`。simulator 删除 plan/legacy attempt fallback，只读取 strict active items 和 action ref。

- [ ] **Step 5: 执行 legacy drain，并形成 Gate 0 artifact**

部署顺序必须由 operator 执行：先上线“新提交 graph_v3 + legacy worker drain-only”，worker allowlist 固定 `execution_engine=legacy_scheduler_v2` 且只处理已存在 rows；观察到零非终态后运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/migrate_legacy_workflows.py \
  migrate-terminal --db .local/workflows/workflows.sqlite3 \
  --backup .data/workflow-m4/workflows-before-m4.sqlite3 \
  --report .data/workflow-m4/migration-report.json
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/migrate_legacy_workflows.py \
  verify-drain --db .local/workflows/workflows.sqlite3 \
  --report .data/workflow-m4/gate-zero.json
```

`verify-drain` 断言：legacy nonterminal=0、legacy terminal=migrated terminal、product owner/status/artifact digest 守恒、graph nonterminal checkpoint 可读、production config=sqlite、backup 存在。任一失败退出非零并禁止 Task 3。

- [ ] **Step 6: 运行 GREEN、真实消费者与 core gate**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_production_workflow_cutover.py \
  tests/tdd/native-langgraph-m4/test_workflow_api_projection.py \
  tests/tdd/native-langgraph-m4/test_legacy_drain_gate.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_durable_lifecycle.py \
  tests/core/contract/test_observability_contract.py
```

Expected: PASS；`LOOP-001` 证明 production Deep Research 由 graph host 执行，`IDENT-001` 证明 thread/run/owner，`OBS-001` 证明 strict 单向 projection；`DUR-001` 原测试原样通过。

- [ ] **Step 7: 运行 production-negative source/AST gate**

```bash
! rg -n "WorkflowService\.submit|claim_ready_work_item|renew_work_item_lease|WorkflowRuntime|AgentRuntimeWorkItemExecutor" \
  src/assistant_agent/runtime/runtime.py src/assistant_agent/api/routes_workflows.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python - <<'PY'
import ast
from pathlib import Path
for name in ('src/assistant_agent/runtime/runtime.py', 'src/assistant_agent/api/routes_workflows.py'):
    tree = ast.parse(Path(name).read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert 'assistant_agent.workflows.runtime' not in imports
    assert 'assistant_agent.workflows.execution' not in imports
PY
```

Expected: 零命中/exit 0。

- [ ] **Step 8: Commit**

```bash
git add src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/runtime/assistant_runtime_app.py \
  src/assistant_agent/workflows/graph_host.py \
  src/assistant_agent/api/app.py src/assistant_agent/api/routes_workflows.py \
  src/assistant_agent/api/models.py \
  src/assistant_agent/runtime/server_startup_summary.py \
  scripts/media_simulator.py scripts/README.md evals/release_review/cli.py \
  tests/tdd/native-langgraph-m4/test_production_workflow_cutover.py \
  tests/tdd/native-langgraph-m4/test_workflow_api_projection.py \
  tests/tdd/native-langgraph-m4/test_legacy_drain_gate.py \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_durable_lifecycle.py \
  tests/core/contract/test_observability_contract.py
git commit -m "feat(workflows): cut production over to graph v3"
```

### Task 3: 停止 legacy worker、删除 old submit composition 与 Workflow OTel 装配

**Gate:** 开始前必须读取 `.data/workflow-m4/gate-zero.json` 并人工确认 `ready=true`、legacy nonterminal=0、backup 可读；这些产物不提交 Git。

**Files:**
- Modify: `src/assistant_agent/api/app.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/runtime/server_startup_summary.py`
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `evals/release_review/cli.py`
- Create: `tests/tdd/native-langgraph-m4/test_legacy_worker_stopped.py`

**Interfaces:**
- Removes: `start_durable_workflow_worker()`、`shutdown_durable_workflow_worker()`、`get_durable_workflow_worker()`、app state worker/stop/task fields、`workflow_service` injection/creation、`create_workflow_otel_observer_from_env()` production composition。
- Keeps: `WorkflowGraphHost` lifecycle、product repository/artifact store、durable task worker lifecycle。
- Real consumer boundary: server startup summary reports graph host/checkpointer readiness instead of legacy worker readiness；Release Review staging still receives workflow DB/checkpoint/artifact paths。
- Breaking cleanup boundary: 删除 `MULTIMODAL_AGENT_DURABLE_WORKFLOW_WORKER_ENABLED` 读取；lease/poll/concurrency config 暂留到 Task 8 source cleanup，防止同 commit 混淆停止与最终 schema 删除。

- [ ] **Step 1: 写 worker-stopped RED**

```python
@pytest.mark.asyncio
async def test_api_lifespan_starts_graph_host_and_never_creates_legacy_worker(app):
    async with app.router.lifespan_context(app):
        assert app.state.workflow_graph_host is not None
        assert not hasattr(app.state, "durable_workflow_worker")
        assert not hasattr(app.state, "durable_workflow_worker_task")
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_legacy_worker_stopped.py
```

Expected: FAIL，lifespan 仍创建 legacy worker state。

- [ ] **Step 3: 删除 production worker/old submit/OTel wiring**

只删除 composition imports/functions/state；本 Task 不删除 `worker.py/runtime.py/execution.py`，以便 commit 可独立证明最后 production consumer 已消失。`AgentGraphRuntime.__init__` 只接收 `workflow_graph_host` 和 artifact store，不创建 `SQLiteWorkflowStore`、`ObservedWorkflowStore` 或 `WorkflowService`。

- [ ] **Step 4: 运行 GREEN 与 import/source gate**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_legacy_worker_stopped.py \
  tests/tdd/native-langgraph-m4/test_production_workflow_cutover.py
! rg -n "DurableWorkflowWorker|WorkflowRuntime|AgentRuntimeWorkItemExecutor|ObservedWorkflowStore|create_workflow_otel_observer_from_env" \
  src/assistant_agent/api/app.py src/assistant_agent/runtime/runtime.py
```

Expected: PASS 且零命中。

- [ ] **Step 5: Commit**

```bash
git add src/assistant_agent/api/app.py src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/runtime/server_startup_summary.py \
  src/assistant_agent/config/__init__.py evals/release_review/cli.py \
  tests/tdd/native-langgraph-m4/test_legacy_worker_stopped.py
git commit -m "refactor(workflows): stop legacy workflow worker"
```

### Task 4: 删除 worker/runtime/execution 与 `AgentGraphRuntime.run_work_item`

**Files:**
- Delete: `src/assistant_agent/workflows/worker.py`
- Delete: `src/assistant_agent/workflows/runtime.py`
- Delete: `src/assistant_agent/workflows/execution.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `src/assistant_agent/workflows/agent_runtime.py`
- Modify: `src/assistant_agent/workflows/context.py`
- Delete or rewrite affected legacy-only tests under: `tests/tdd/durable-workflows/`
- Create: `tests/tdd/native-langgraph-m4/test_legacy_executor_deleted.py`

**Interfaces:**
- Removes: `DurableWorkflowWorker`、`WorkflowRuntime.run_claim()`、`WorkItemAssignment`、`WorkItemExecutionResult`、`WorkItemExecutor`、`AgentRuntimeWorkItemExecutor.execute()`、`BoundedAgentRuntime.run_work_item()`、`AgentGraphRuntime.run_work_item()`。
- Keeps: planner/worker/verifier `AssistantTurnGraph` profiles in `durable_graph_nodes.py` and `graph_context.py`；这些 graph-native branch 不依赖 deleted executor。
- Real consumer boundary: M3 native graph and LangSmith experiment directly run compiled subgraphs；no API/media consumer imports removed classes。
- Breaking cleanup boundary: legacy unit/TDD tests whose sole subject is deleted scheduler are removed/replaced by deletion gates；owner/artifact/graph product tests stay。

- [ ] **Step 1: 写 delete RED**

```python
def test_legacy_executor_modules_and_runtime_entry_are_absent():
    for module in (
        "assistant_agent.workflows.worker",
        "assistant_agent.workflows.runtime",
        "assistant_agent.workflows.execution",
    ):
        assert importlib.util.find_spec(module) is None
    assert not hasattr(AgentGraphRuntime, "run_work_item")
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_legacy_executor_deleted.py
```

Expected: FAIL，三个模块和 method 仍存在。

- [ ] **Step 3: 删除实现并收缩仍共用的 branch DTO**

删除文件与 import；`agent_runtime.py` 只保留 graph branch 真正消费的 request/result parsing；若某类型仅被 deleted executor 使用则同 commit 删除。`context.py` 保留 artifact/context compilation 的 graph-native consumer，不保留 executor adapter。

- [ ] **Step 4: 运行 GREEN、全仓 AST consumer gate 与 graph regression**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_legacy_executor_deleted.py \
  tests/tdd/native-langgraph-m3/test_workflow_send_join.py \
  tests/tdd/native-langgraph-m3/test_workflow_verify_repair.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python - <<'PY'
import ast
from pathlib import Path
for path in Path('.').rglob('*.py'):
    if '.worktrees' in path.parts:
        continue
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {
                'assistant_agent.workflows.worker',
                'assistant_agent.workflows.runtime',
                'assistant_agent.workflows.execution',
            }, path
PY
test ! -e src/assistant_agent/workflows/worker.py
test ! -e src/assistant_agent/workflows/runtime.py
test ! -e src/assistant_agent/workflows/execution.py
```

Expected: PASS、无 import、三个文件不存在。

- [ ] **Step 5: Commit**

```bash
git add -A src/assistant_agent/workflows/worker.py \
  src/assistant_agent/workflows/runtime.py \
  src/assistant_agent/workflows/execution.py \
  src/assistant_agent/runtime/runtime.py \
  src/assistant_agent/workflows/agent_runtime.py \
  src/assistant_agent/workflows/context.py \
  tests/tdd/durable-workflows \
  tests/tdd/native-langgraph-m4/test_legacy_executor_deleted.py
git commit -m "refactor(workflows): delete legacy work item executor"
```

### Task 5: 删除 claim/renew/recover/ready/CAS/lease 模型并重建业务 DB schema

**Files:**
- Modify: `src/assistant_agent/workflows/models.py`
- Modify: `src/assistant_agent/workflows/store.py`
- Modify: `src/assistant_agent/workflows/sqlite_store.py`
- Modify: `src/assistant_agent/workflows/transitions.py`
- Modify: `src/assistant_agent/workflows/definitions.py`
- Modify: `src/assistant_agent/workflows/constraints.py`
- Modify: `src/assistant_agent/workflows/graph_state.py`
- Modify: `src/assistant_agent/workflows/legacy_migration.py`
- Modify: `scripts/migrate_legacy_workflows.py`
- Create: `tests/tdd/native-langgraph-m4/test_scheduler_state_deleted.py`
- Create: `tests/tdd/native-langgraph-m4/test_workflow_schema_rebuild.py`

**Interfaces:**
- Removes functions: `workflow_matches_claim_scope()`、`recover_expired_work_item_leases()`、`claim_ready_item_in_bundle()`、`_clear_item_lease()`、`_lease_matches()`、`WorkflowStore.save(expected_revision=...)`、`claim_ready_work_item()`、`renew_work_item_lease()`。
- Removes models/fields: `WorkflowDispatch`、`WorkflowWorkItemLease`、`WorkItemStatus.ready/running/retryable_failed` scheduler semantics、`active_attempt_id`、`lease_owner`、`lease_token`、`lease_expires_at`、`reserved_model_calls`、`reserved_tool_calls`；legacy `revision` CAS only when it has no product consumer。
- DB migration: copy verified `workflow_products` and `workflow_product_events` to new tables, validate counts/digests, then rebuild/drop legacy `durable_workflows`、`durable_workflow_events` and `idx_durable_workflows_claim` in one explicit migration transaction after backup。
- Keeps: `WorkflowSubmission`、v2 admitted plan domain types still consumed by graph state/planner、owner/idempotency/artifact/audit product facts；checkpointer owns execution status, generation and pending tasks。
- Real consumer boundary: API reads product repository; graph uses `PersistedAdmittedWorkflowPlan` in checkpoint; no consumer reads SQL ready/lease/CAS columns。

- [ ] **Step 1: 写 scheduler-state 与 schema RED**

```python
def test_workflow_models_have_no_scheduler_lease_fields():
    forbidden = {
        "active_attempt_id", "lease_owner", "lease_token", "lease_expires_at",
        "reserved_model_calls", "reserved_tool_calls",
    }
    assert forbidden.isdisjoint(WorkflowWorkItem.model_fields)
    assert importlib.util.find_spec("assistant_agent.workflows.models") is not None
    assert not hasattr(workflow_models, "WorkflowDispatch")
    assert not hasattr(workflow_models, "WorkflowWorkItemLease")


def test_schema_rebuild_drops_claim_columns_and_preserves_product_rows(migrated_db):
    before = _product_digest(migrated_db)
    rebuild_workflow_schema(migrated_db)
    assert _table_columns(migrated_db, "workflow_products").isdisjoint(
        {"lease_owner", "lease_token", "lease_expires_at", "bundle_json"}
    )
    assert "idx_durable_workflows_claim" not in _index_names(migrated_db)
    assert _product_digest(migrated_db) == before
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_scheduler_state_deleted.py \
  tests/tdd/native-langgraph-m4/test_workflow_schema_rebuild.py
```

Expected: FAIL，scheduler symbols/columns/indexes 仍存在。

- [ ] **Step 3: 删除 Python scheduler state 并改 graph admission adapter**

`initial_workflow_graph_state()` 不再要求 legacy `WorkflowRecord`/`WorkflowPlanVersion` Bundle；改为消费 `WorkflowProductRecord` + `PersistedAdmittedWorkflowPlan | None`。Graph budget/generation 留在 checkpoint DTO，不反写 SQL reservation/attempt 字段。

- [ ] **Step 4: 实现显式 DB rebuild migration**

`rebuild-schema` CLI 只在 `verify-drain` report ready、backup 可读且旧 nonterminal=0 时运行；事务内：创建 `_workflow_products_m4`/`_workflow_product_events_m4`、严格 deserialize+insert、核对 row count/status/owner/artifact/event digest、rename 到正式表、drop legacy tables/index。任何校验失败 rollback，不删除 backup。

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/migrate_legacy_workflows.py \
  rebuild-schema --db .local/workflows/workflows.sqlite3 \
  --gate-report .data/workflow-m4/gate-zero.json \
  --backup .data/workflow-m4/workflows-before-schema-rebuild.sqlite3
```

- [ ] **Step 5: 运行 GREEN、symbol/source/schema gate**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_scheduler_state_deleted.py \
  tests/tdd/native-langgraph-m4/test_workflow_schema_rebuild.py \
  tests/tdd/native-langgraph-m3/test_workflow_graph_state.py
! rg -n "claim_ready_work_item|renew_work_item_lease|recover_expired_work_item_leases|claim_ready_item_in_bundle|WorkflowWorkItemLease|WorkflowDispatch|lease_owner|lease_token|lease_expires_at|reserved_model_calls|reserved_tool_calls|idx_durable_workflows_claim" \
  src/assistant_agent/workflows scripts/migrate_legacy_workflows.py
```

Expected: PASS 且零命中。命令 scope 不含 `automation/durable_tasks`，避免把受保护同名 lease 错判为待删。

- [ ] **Step 6: Commit**

```bash
git add src/assistant_agent/workflows/models.py \
  src/assistant_agent/workflows/store.py \
  src/assistant_agent/workflows/sqlite_store.py \
  src/assistant_agent/workflows/transitions.py \
  src/assistant_agent/workflows/definitions.py \
  src/assistant_agent/workflows/constraints.py \
  src/assistant_agent/workflows/graph_state.py \
  src/assistant_agent/workflows/legacy_migration.py \
  scripts/migrate_legacy_workflows.py \
  tests/tdd/native-langgraph-m4/test_scheduler_state_deleted.py \
  tests/tdd/native-langgraph-m4/test_workflow_schema_rebuild.py
git commit -m "refactor(workflows): remove scheduler lease state"
```

### Task 6: 删除 legacy planning/progress/raw Bundle 与旧 service/store façade

**Files:**
- Delete: `src/assistant_agent/workflows/planning.py`
- Delete: `src/assistant_agent/workflows/progress.py`
- Delete: `src/assistant_agent/workflows/service.py`
- Delete: `src/assistant_agent/workflows/store.py`
- Delete: `src/assistant_agent/workflows/sqlite_store.py`
- Modify: `src/assistant_agent/workflows/models.py`
- Modify: `src/assistant_agent/workflows/definitions.py`
- Modify: `src/assistant_agent/workflows/graph_state.py`
- Modify: `src/assistant_agent/workflows/graph_projection.py`
- Modify: `src/assistant_agent/api/routes_workflows.py`
- Create: `tests/tdd/native-langgraph-m4/test_raw_bundle_deleted.py`

**Interfaces:**
- Removes: `next_ready_work_item()`、`project_workflow_progress(WorkflowRecord, WorkflowPlanVersion)`、`WorkflowService`、legacy `WorkflowStore`、`WorkflowBundle`、`WorkflowPlanVersion.current_plan` 与 raw Bundle API path。
- Keeps: `WorkflowSubmission`、planner proposal/admitted-plan domain schema that graph nodes truly import、strict product repository、graph projector、artifact store。
- Real consumer boundary: media simulator already consumes `WorkflowProductProgress`; API no longer imports legacy progress/service; LangSmith target consumes graph state。
- Breaking cleanup boundary: delete legacy v1 `WorkflowPlanProposal` and bootstrap materialization only after `rg` proves no graph/eval consumer；v2 proposal/admission types stay even if they move to a focused `plan_models.py`。

- [ ] **Step 1: 写 raw Bundle delete RED**

```python
def test_raw_bundle_and_legacy_facades_are_absent():
    assert not hasattr(workflow_models, "WorkflowBundle")
    for module in (
        "assistant_agent.workflows.planning",
        "assistant_agent.workflows.progress",
        "assistant_agent.workflows.service",
        "assistant_agent.workflows.store",
        "assistant_agent.workflows.sqlite_store",
    ):
        assert importlib.util.find_spec(module) is None
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_raw_bundle_deleted.py
```

Expected: FAIL，legacy modules/Bundle 仍存在。

- [ ] **Step 3: 重定向最后 graph domain imports 后删除 façade**

先用 `rg -l` 列出每个 legacy model consumer，逐一分类为“移动到 focused plan/product model”或“随 legacy 删除”；禁止创建 `legacy_compat.py` 复制 raw Bundle。删除前 API、host、migration 均已只依赖 strict repository。

- [ ] **Step 4: 运行 GREEN、AST/delete gate 与 API regression**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_raw_bundle_deleted.py \
  tests/tdd/native-langgraph-m4/test_workflow_api_projection.py \
  tests/tdd/native-langgraph-m3/test_workflow_planning_subgraph.py
for path in planning.py progress.py service.py store.py sqlite_store.py; do
  test ! -e "src/assistant_agent/workflows/$path"
done
! rg -n "WorkflowBundle|next_ready_work_item|project_workflow_progress" \
  src/assistant_agent scripts evals tests/core tests/tdd/native-langgraph-m4
```

Expected: PASS、五文件不存在、零 legacy symbol consumer。

- [ ] **Step 5: Commit**

```bash
git add -A src/assistant_agent/workflows src/assistant_agent/api/routes_workflows.py \
  tests/tdd/native-langgraph-m4/test_raw_bundle_deleted.py
git commit -m "refactor(workflows): remove raw workflow bundle facades"
```

### Task 7: 删除 ObservedWorkflowStore、Workflow OTel/trace 影子树

**Files:**
- Delete: `src/assistant_agent/workflows/observed_store.py`
- Delete: `src/assistant_agent/observability/workflow_otel.py`
- Delete: `src/assistant_agent/observability/workflow_trace.py`
- Modify: `src/assistant_agent/observability/__init__.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `docs/observability-harness.md`
- Modify: `evals/README.md`
- Modify or delete affected tests under: `tests/tdd/durable-workflows/`
- Create: `tests/tdd/native-langgraph-m4/test_workflow_shadow_observability_deleted.py`
- Modify: `tests/core/contract/test_observability_contract.py`

**Interfaces:**
- Removes: `ObservedWorkflowStore`、`WorkflowCommitObserver`、`WorkflowOTelObserver`、`build_workflow_otel_span_specs()`、`workflow_root_span_id()` 以及由 legacy event/lease 重建的 workflow/work-item span tree。
- Keeps: LangSmith native compiled graph/node/subgraph/LLM/governed Tool tracing；canonical local business audit、trace ledger/query、runtime audit、delivery audit、non-Workflow Langfuse/OTel compatibility。
- Real consumer boundary: M3/M4 LangSmith experiment validates actual graph hierarchy；product API does not expose trace tree。
- Breaking cleanup boundary: 不为了旧 Langfuse dashboard 保留 shadow spans；M5 才删除 Langfuse runner/webhook/exporter 的其余部分。

- [ ] **Step 1: 写 shadow observability delete RED**

```python
def test_legacy_workflow_shadow_modules_are_absent_and_native_trace_remains():
    for module in (
        "assistant_agent.workflows.observed_store",
        "assistant_agent.observability.workflow_otel",
        "assistant_agent.observability.workflow_trace",
    ):
        assert importlib.util.find_spec(module) is None
    assert native_workflow_trace_probe().root_name == "DurableWorkflowGraph"
    assert native_workflow_trace_probe().has_manual_otel_parent is False
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_workflow_shadow_observability_deleted.py
```

Expected: FAIL，三个影子模块仍存在。

- [ ] **Step 3: 删除影子模块与旧 tests/docs claims**

文档明确：Workflow 新增 trace 只由 LangSmith native graph tree 提供；业务 product events 与 artifact audit 仍为独立事实，不伪装 node；不声称通用 Langfuse 已退出。

- [ ] **Step 4: 运行 GREEN、全仓 import/delete gate 与 OBS-001**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_workflow_shadow_observability_deleted.py \
  tests/tdd/native-langgraph-m3/test_langsmith_workflow_experiment.py \
  tests/core/contract/test_observability_contract.py
test ! -e src/assistant_agent/workflows/observed_store.py
test ! -e src/assistant_agent/observability/workflow_otel.py
test ! -e src/assistant_agent/observability/workflow_trace.py
! rg -n "ObservedWorkflowStore|workflow_otel|workflow_root_span_id|build_workflow_otel_span_specs" \
  src scripts evals tests/core tests/tdd/native-langgraph-m4
```

Expected: PASS、文件不存在、零 import/symbol consumer。

- [ ] **Step 5: Commit**

```bash
git add -A src/assistant_agent/workflows/observed_store.py \
  src/assistant_agent/observability/workflow_otel.py \
  src/assistant_agent/observability/workflow_trace.py \
  src/assistant_agent/observability/__init__.py \
  src/assistant_agent/runtime/runtime.py docs/observability-harness.md \
  evals/README.md tests/tdd/durable-workflows \
  tests/tdd/native-langgraph-m4/test_workflow_shadow_observability_deleted.py \
  tests/core/contract/test_observability_contract.py
git commit -m "refactor(observability): delete workflow shadow trace tree"
```

### Task 8: Sunset `long_horizon`/legacy config、authority 同步与 M4 最终删除验收

**Files:**
- Delete: `src/assistant_agent/workflows/builtin.py` if it only contains `LongHorizonWorkflowDefinition`
- Modify: `src/assistant_agent/workflows/definitions.py`
- Delete: `src/assistant_agent/workflows/research/definition.py` if graph planning no longer imports the legacy class
- Modify: `src/assistant_agent/config/__init__.py`
- Modify: `src/assistant_agent/runtime/server_startup_summary.py`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/runtime-event-stream-architecture.md`
- Modify: `docs/observability-harness.md`
- Modify: `docs/authority.toml` only if source ownership globs/verification commands changed
- Modify: `README.md` and `scripts/README.md` only for current human navigation/commands
- Create: `tests/tdd/native-langgraph-m4/test_m4_deletion_gates.py`
- Modify: `tests/core/integration/test_runtime_lifecycle.py`
- Modify: `tests/core/integration/test_durable_lifecycle.py`
- Modify: `tests/core/contract/test_observability_contract.py`

**Interfaces:**
- Removes: `LongHorizonWorkflowDefinition`、`default_workflow_definitions()` legacy catalog、`legacy_scheduler_v2` literal/migration default、worker enable/lease/poll/concurrency env/config and startup status。
- Keeps: `durable_workflows_enabled`、workflow product DB path、checkpoint DB path、artifact path、graph execution budget/config that native graph actually consumes；field-by-field `rg` decides, not prefix deletion。
- Real consumer boundary: only `assistant_mode=deep_research -> DurableWorkflowGraph` remains; no product/API consumer could select `long_horizon` before M4。Release Review retains graph/checkpoint/artifact readiness。
- Breaking cleanup boundary: no alias or hidden fallback for `long_horizon`/`legacy_scheduler_v2`; old deployment config using removed worker env must fail validation or be reported as unknown during one release, never silently re-enable a legacy path。

- [ ] **Step 1: 写 final deletion gate RED**

```python
def test_m4_forbidden_symbols_and_files_are_absent():
    forbidden_files = {
        "src/assistant_agent/workflows/worker.py",
        "src/assistant_agent/workflows/runtime.py",
        "src/assistant_agent/workflows/execution.py",
        "src/assistant_agent/workflows/planning.py",
        "src/assistant_agent/workflows/progress.py",
        "src/assistant_agent/workflows/observed_store.py",
        "src/assistant_agent/observability/workflow_otel.py",
        "src/assistant_agent/observability/workflow_trace.py",
    }
    assert all(not Path(path).exists() for path in forbidden_files)
    assert _source_hits("legacy_scheduler_v2|long_horizon|DurableWorkflowWorker") == []


def test_protected_durable_tasks_remain_importable_and_functional():
    from assistant_agent.automation.durable_tasks.service import DurableTaskService
    from assistant_agent.automation.durable_tasks.worker import DurableTaskWorker
    assert DurableTaskService is not None and DurableTaskWorker is not None
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4/test_m4_deletion_gates.py
```

Expected: FAIL，legacy definition/config/literals 尚未全部 sunset。

- [ ] **Step 3: 删除 long_horizon、legacy engine 与 worker config**

从 `ProviderConfig` 和 env loader 删除：

```text
durable_workflow_worker_enabled / MULTIMODAL_AGENT_DURABLE_WORKFLOW_WORKER_ENABLED
durable_workflow_lease_seconds / MULTIMODAL_AGENT_DURABLE_WORKFLOW_LEASE_SECONDS
durable_workflow_poll_seconds / MULTIMODAL_AGENT_DURABLE_WORKFLOW_POLL_SECONDS
durable_workflow_max_concurrent_items / MULTIMODAL_AGENT_DURABLE_WORKFLOW_MAX_CONCURRENT_ITEMS
```

删除 `WorkflowExecutionEngine` 双值 literal；严格 graph/product schema 不再需要 engine discriminator 时，同时删除 product DB engine 列并用小型 rebuild migration核对全表均曾为 `langgraph_v3`。保留 native graph 实际消费的 `durable_workflow_max_quanta`。

- [ ] **Step 4: 同步 authority 与当前文档**

`docs/tool-calling-architecture.md` 写当前事实：Workflow v2 domain semantics + LangGraph execution、无 legacy scheduler、`automation.durable_tasks` 独立；`runtime-event-stream-architecture.md` 写 saver/host/strict projection/recovery；`observability-harness.md` 写 native LangSmith 和删除 shadow tree。删除 M3/M4 sunset 语言，不把历史计划当 authority。

- [ ] **Step 5: 运行 final GREEN、source/AST/delete/durable-task gates**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-langgraph-m4 \
  tests/tdd/native-langgraph-m3 \
  tests/tdd/native-langgraph-m2 \
  tests/tdd/native-langgraph-runtime
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
! rg -n "legacy_scheduler_v2|long_horizon|DurableWorkflowWorker|WorkflowRuntime|AgentRuntimeWorkItemExecutor|claim_ready_work_item|renew_work_item_lease|next_ready_work_item|ObservedWorkflowStore|workflow_otel|workflow_root_span_id" \
  src/assistant_agent/workflows src/assistant_agent/runtime src/assistant_agent/api \
  src/assistant_agent/observability src/assistant_agent/config scripts evals
rg -n "automation\.durable_tasks|DurableTaskService|DurableTaskWorker" \
  src/assistant_agent tests/core/integration/test_durable_lifecycle.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/workflows src/assistant_agent/runtime src/assistant_agent/api \
  src/assistant_agent/observability tests/tdd/native-langgraph-m4
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent tests/tdd/native-langgraph-m4
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
git diff --check
```

Expected: TDD/core PASS；legacy source gate 零命中；durable task positive gate 有命中且 `DUR-001` PASS；ruff/compileall/authority/diff 均通过。若 `review_required` 包含 test-policy/owner，只表示按 `tests/README.md` 和对应 authority 完成复核，不机械制造额外文档 diff。

- [ ] **Step 6: 运行真实 production acceptance（operator gate）**

使用 staging 隔离 SQLite checkpoint/product/artifact 路径，显式 real mode、Provider/LangSmith readiness 和 operator flags 运行现有 LangSmith Workflow Experiment；必须证明 submit、fan-out/join、interrupt/restart/resume、repair、publish、API tail、final artifact、native trace tree 和四项 Feedback。报告同时记录：无 legacy worker process/thread、无 legacy rows/tables/index、无 Workflow shadow OTel spans、无重复副作用；缺任一项则 M4 不 complete。

- [ ] **Step 7: Commit**

```bash
git add -A src/assistant_agent/workflows src/assistant_agent/config/__init__.py \
  src/assistant_agent/runtime/server_startup_summary.py \
  docs/tool-calling-architecture.md docs/runtime-event-stream-architecture.md \
  docs/observability-harness.md docs/authority.toml README.md scripts/README.md \
  tests/tdd/native-langgraph-m4 \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/integration/test_durable_lifecycle.py \
  tests/core/contract/test_observability_contract.py
git commit -m "refactor(workflows): complete native graph migration"
```

## 最终验收与汇报格式

实施者只有在 Task 0–8 全部 review 通过后才能写 `M4 complete`。最终报告必须包含：

```text
Gate 0: passed; persistent saver=sqlite/3.1.0; graph_v3 new submissions 使用
gate-zero.json 中的 observed integer；legacy nonterminal=0；terminal migrated 使用
migration-report.json 中的 observed integer；backup 使用 gate-zero.json 的绝对路径；recovery=passed.
Core invariant: LOOP-001, IDENT-001 and OBS-001 updated for production WorkflowGraphHost;
DUR-001 unchanged and passed.
Tests: added/updated tests/tdd/native-langgraph-m4 for temporary RED/GREEN;
user may delete that directory manually after acceptance.
Protected boundary: src/assistant_agent/automation/durable_tasks/** unchanged and DUR-001 passed.
Real Provider/LangSmith: 未调用时明确写“未调用”；已调用时列出 staging run ID、
Experiment ID、四项 Feedback 的实际结果与 operator gate。
Deleted: 逐项列出本计划实际删除的文件、symbol、config、DB table 和 index；不得只写汇总数量。
Remaining: M5-only Langfuse/general Runtime convergence items.
```

不得把以下任一情况表述为完成：只用 `InMemorySaver`、persistent test 被 skip、legacy rows 尚有非终态、只停止 worker 但保留可达 submit、只删文件但仍有 AST import、API 仍返回 raw Bundle/plan、shadow OTel 仍重建 Workflow tree、或 durable task core 未通过。
