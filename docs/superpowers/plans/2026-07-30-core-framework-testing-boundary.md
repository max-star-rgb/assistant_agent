# 核心框架测试边界实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将默认 pytest 收敛为只保护稳定框架不变量的 `tests/core/`，允许功能开发在可手动删除的
`tests/tdd/<feature>/` 做临时 RED/GREEN，并把需长期显式运行的具体节点和实现级检查迁入可独立删除的
system eval incubating 目录。

**Architecture:** `pyproject.toml` 只收集 `tests/core`；核心用例以 invariant ID 登记并通过收集门禁校验。
临时 TDD 只位于 `tests/tdd/<feature>/`、显式运行并强制 mock/offline，不要求 invariant marker，也不自动
晋升 core。现有业务节点、Provider、Agent Task 和需保留的实现级 pytest 按能力域迁入
`evals/system/incubating/<feature>/`，不进入默认发布门禁。

**Tech Stack:** Python 3.11+、pytest 8、Pydantic v2、现有 mock/scripted/in-memory adapter、Markdown 项目权威和 Codex project skill。

## Global Constraints

- 默认 Python 固定使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- 默认 pytest 只能使用 mock/local/offline，不调用真实 Provider、MCP、Mem0、天气、搜索或付费服务。
- 裸 pytest 只收集 `tests/core`，目标运行时间不超过 60 秒。
- `tests/tdd/<feature>/` 只能显式运行、强制 mock/offline；Codex 不得擅自删除，用户完成后可手动整目录删除。
- 核心测试不导入具体 builtin Tool、具体 Provider 实现、Agent Task 或 grader。
- 核心测试不精确绑定完整回复、prompt、Tool description、console output 或供应商 payload。
- 新增 Tool、Provider、业务节点或 Agent Task 后，核心测试数量和预期结果默认保持不变。
- 不回滚当前工作区的源码、eval、配置和文档改动；迁移未提交测试前必须保留其内容。
- 本计划不调用真实 Provider，不安装新依赖，不 push、不合并、不创建 PR。
- 当前工作区高度脏且测试文件与其他任务重叠，本轮不自动提交；完成后只报告本任务 diff。

---

## 文件结构与迁移映射

### 新建核心文件

```text
tests/core/
  INVARIANTS.md
  conftest.py
  support.py
  unit/test_test_policy.py
  contract/test_gateway_contract.py
  contract/test_observability_contract.py
  contract/test_tool_contract.py
  integration/test_context_lifecycle.py
  integration/test_durable_lifecycle.py
  integration/test_memory_lifecycle.py
  integration/test_runtime_lifecycle.py

tests/tdd/
  README.md
  conftest.py
  <feature>/
    test_*.py
```

### Incubating 能力域

| 目录 | 接收的现有测试 |
| --- | --- |
| `agent-eval-infrastructure/` | `tests/contract/evals/*.py`、`tests/integration/eval/*.py` |
| `shopping/` | `tests/integration/shopping/*.py` |
| `workspace-tools/` | email、calendar、contacts、local file 测试 |
| `lodging/` | lodging contract、FlyAI adapter 测试 |
| `mcp/` | MCP config、registration、SDK environment 测试 |
| `image-generation/` | image generation provider/tool 测试 |
| `web-and-media-tools/` | Tavily、visual media boundary 测试 |
| `provider-observability/` | Langfuse、OTel、provider streaming、provider trace 测试 |
| `runtime-features/` | hotel watch、realtime response/revision、extended runtime、long-running scenario 测试 |
| `memory-provider/` | Mem0 lifecycle、session snapshot 中 Provider 专属验证 |
| `context-features/` | tokenizer、完整 prompt projection、自然语言 compaction 展示测试 |
| `server-operations/` | server startup、dependency console、skill HTTP contract 测试 |
| `tool-extension-features/` | builtin ownership、media/python 专项治理、plugin assembly/runtime 测试 |
| `gateway-media-adapter/` | Media-Agent interrupt vendor envelope 测试 |

每个目录使用 `checks_*.py`，只能显式运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  evals/system/incubating/<feature>/checks_*.py
```

---

### Task 1: 建立核心不变量登记与 pytest 硬门禁

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/core/INVARIANTS.md`
- Create: `tests/core/conftest.py`
- Create: `tests/core/unit/test_test_policy.py`
- Create: `tests/tdd/README.md`
- Create: `tests/tdd/conftest.py`

**Interfaces:**
- Consumes: 已批准设计中的核心不变量分组。
- Produces: `core_invariant(<ID>)` marker、已登记 ID 集合、默认收集路径、临时 TDD 边界和目录/文案/import 策略检查。

- [ ] **Step 1: 创建不变量登记文件**

在 `tests/core/INVARIANTS.md` 建立以下稳定条目，并为每条写出结构化契约及负责文件：

```text
POLICY-001  默认 pytest 只允许核心测试
BOOT-001    mock/real 与离线启动边界
RUN-001     Run 完成、失败、取消和终态
LOOP-001    通用 assistant loop 与 tool call 循环
TOOL-001    Tool validation/execution/registry 治理链
EXT-001     Probe Tool/Plugin 扩展契约
CTX-001     Context budget、compaction 与因果配对
GATE-001    Gateway session/run/turn/frame 生命周期
IDENT-001   user/session/agent/run 身份隔离
DUR-001     Durable schedule/resume/cancel/outbox 状态机
OBS-001     canonical event、trace correlation 与终态可见性
```

表格第一列必须是 ID，第三列必须列出负责的 `tests/core/...` 文件，供收集门禁解析。

- [ ] **Step 2: 先写策略测试**

`tests/core/unit/test_test_policy.py` 使用 `pathlib` 和 `ast` 实现四个聚焦测试：

```python
@pytest.mark.core_invariant("POLICY-001")
def test_python_tests_exist_only_under_core_or_tdd_features() -> None:
    offenders = [
        path
        for path in TESTS_ROOT.rglob("*.py")
        if _is_pytest_file(path) and not _is_allowed_pytest_path(path)
    ]
    assert offenders == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_test_files_are_registered() -> None:
    assert _unregistered_core_test_files() == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_assertions_do_not_bind_human_copy() -> None:
    assert _human_copy_assertion_offenders() == []


@pytest.mark.core_invariant("POLICY-001")
def test_core_tests_do_not_import_feature_implementations() -> None:
    assert _forbidden_import_offenders() == []
```

`_human_copy_assertion_offenders()` 扫描 `ast.Assert` 中的字符串常量；包含中文或空白的字符串均视为
完整文案，`*-sentinel`、稳定协议 token、error code 和 invariant ID 除外。

`_unregistered_core_test_files()` 要求每个 `tests/core/**/test_*.py` 相对路径都出现在
`INVARIANTS.md` 的负责文件列中，防止通过复用已有 ID 随意创建新测试文件。

策略允许 `test_*.py` 和 `*_test.py` 位于 `tests/tdd/<feature>/`，但拒绝在 `tests/tdd/` 根目录
直接放测试。TDD 测试不要求 invariant 登记或 marker，且不得进入默认收集。

`_forbidden_import_offenders()` 至少拒绝：

```text
assistant_agent.tools.plugins.builtin
evals.agent
assistant_agent.providers.qwen
assistant_agent.providers.deepseek
assistant_agent.memory.mem0
```

- [ ] **Step 3: 运行策略测试验证旧结构会失败**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/unit/test_test_policy.py
```

Expected: FAIL，报告现有 `tests/contract`、`tests/integration`、`tests/unit` 中的旁路 pytest。

- [ ] **Step 4: 实现 collection gate**

`tests/core/conftest.py` 从 `INVARIANTS.md` 解析 `^[A-Z]+-[0-9]{3}$` ID，并实现：

```python
def pytest_collection_modifyitems(config, items) -> None:
    for item in items:
        if CORE_ROOT not in Path(str(item.path)).resolve().parents:
            continue
        marker = item.get_closest_marker("core_invariant")
        if marker is None or len(marker.args) != 1:
            raise pytest.UsageError(f"{item.nodeid}: missing core_invariant marker")
        invariant_id = str(marker.args[0])
        if invariant_id not in registered_invariant_ids():
            raise pytest.UsageError(
                f"{item.nodeid}: unknown core invariant {invariant_id}"
            )
```

解析函数只读取仓库内 `tests/core/INVARIANTS.md`，不访问网络或环境配置。

- [ ] **Step 5: 修改 pytest 配置**

保留 `pyproject.toml` 当前未提交的 `tqdm` 依赖改动，只修改 pytest 段：

```toml
[tool.pytest.ini_options]
testpaths = ["tests/core"]
pythonpath = ["src"]
markers = [
  "core_invariant(id): stable core-framework invariant identifier",
]
```

- [ ] **Step 6: 验证收集门禁**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  --collect-only -q tests/core
```

Expected: 新策略测试可以收集；未知或缺失 marker 会产生 `pytest.UsageError`。

- [ ] **Step 7: 验证临时 TDD feature**

创建临时 `tests/tdd/<feature>/test_probe.py`，证明裸 collect 不包含它、显式路径可收集运行且强制
mock/offline；再同时显式收集一个 core item 与该 TDD feature，证明只有 core item 需要 invariant
marker。验证后删除 probe，保留 `tests/tdd/README.md` 与 `tests/tdd/conftest.py`。

---

### Task 2: 建立 Runtime、Tool 和 Gateway 核心安全网

**Files:**
- Create: `tests/core/support.py`
- Create: `tests/core/integration/test_runtime_lifecycle.py`
- Create: `tests/core/contract/test_tool_contract.py`
- Create: `tests/core/contract/test_gateway_contract.py`
- Source references:
  - `tests/integration/runtime/test_safety_net.py`
  - `tests/contract/tools/test_tool_governance.py`
  - `tests/contract/tools/test_tool_observation_contract.py`
  - `tests/contract/gateway/test_gateway_turn_modes.py`
  - `tests/contract/gateway/test_gateway_connection_lease.py`

**Interfaces:**
- Consumes: `core_invariant` marker gate。
- Produces: 与任何具体业务节点无关的 Probe Tool、scripted chat、Runtime 生命周期、Tool 治理和 Gateway 生命周期证明。

- [ ] **Step 1: 创建通用测试支持类型**

`tests/core/support.py` 只定义无业务语义的 fake：

```python
class ProbeInput(BaseModel):
    value: str = Field(min_length=1)


class ProbeTool(ToolBase):
    name = "probe_tool"
    description = "probe-sentinel"
    input_schema = ProbeInput
    output_schema = ProbeInput
    category = "read"

    def _run(self, input: ProbeInput, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            success=True,
            data={"value": input.value},
        )


class ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)
```

同时提供 `offline_config()`、`CancelledToken` 和创建 sealed `ToolRegistry` 的 helper。

- [ ] **Step 2: 创建 Runtime 生命周期测试**

从 `test_safety_net.py` 合并并改写以下不变量，不导入 Mem0 具体实现：

```text
test_runtime_initializes_offline
test_plain_text_run_reaches_completed_terminal_state
test_entry_run_and_agent_identity_are_preserved
test_probe_tool_call_completes_through_governed_runtime
test_provider_timeout_returns_structured_terminal_reason
test_cancelled_run_emits_no_final_response
test_core_event_reaches_gateway_frame
test_session_identity_isolated_by_user_and_agent
```

使用 `BOOT-001`、`RUN-001`、`LOOP-001`、`TOOL-001`、`GATE-001`、
`IDENT-001`、`OBS-001` marker。回复只断言存在性和结构化
`fallback_reason`，不比较完整文案。

- [ ] **Step 3: 创建通用 Tool 契约测试**

只保留与 Probe Tool 有关的治理不变量：

```text
test_runtime_owned_fields_are_hidden_and_bound_per_run
test_model_cannot_submit_runtime_owned_fields
test_available_catalog_is_the_execution_allowlist
test_validated_input_is_reused_by_executor
test_executor_reports_wall_latency
test_write_execution_has_one_structured_terminal_result
test_llm_selected_probe_is_not_filtered_by_request_text
test_tool_owned_validator_runs_without_tool_name_branch
test_success_observation_separates_status_summary_and_data
test_failed_observation_uses_structured_error
test_removed_observation_fields_are_rejected
```

所有 summary、error message 和数据内容使用 `summary-sentinel`、`error-sentinel`、
`value-sentinel`，不导入 builtin shopping、media、python、MCP 或 memory Tool。

- [ ] **Step 4: 创建 Gateway 核心契约测试**

合并 turn mode 与 connection lease 的六条稳定行为：

```text
test_followup_queues_without_interrupting_active_run
test_replace_cancels_before_replacement
test_invalid_turn_mode_is_rejected_before_runtime
test_new_connection_takes_over_without_cancelling_run
test_detached_connection_replays_outbox_after_cursor
test_hangup_destroys_logical_session
```

请求文本改成 `first-sentinel`、`second-sentinel`；只断言 frame type、run/session ID、错误码和状态顺序。

- [ ] **Step 5: 运行三组核心测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_runtime_lifecycle.py \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_gateway_contract.py
```

Expected: PASS；没有具体 builtin 或 Agent Task import。

---

### Task 3: 建立 Context、Durable、Memory 和 Observability 核心安全网

**Files:**
- Create: `tests/core/integration/test_context_lifecycle.py`
- Create: `tests/core/integration/test_durable_lifecycle.py`
- Create: `tests/core/integration/test_memory_lifecycle.py`
- Create: `tests/core/contract/test_observability_contract.py`
- Source references:
  - `tests/integration/context/test_rolling_context_compaction.py`
  - `tests/integration/context/test_context_compiled_accounting.py`
  - `tests/integration/runtime/test_durable_task_schedule.py`
  - `tests/integration/runtime/test_durable_task_event_subscription.py`
  - `tests/integration/runtime/test_durable_task_notification_outbox.py`
  - `tests/integration/runtime/test_durable_task_proactive_resume.py`
  - `tests/integration/runtime/test_proactive_wake_identity_migration.py`
  - `tests/integration/memory/test_memory_ingestion_background.py`
  - `tests/contract/observability/test_assistant_turn_output.py`
  - `tests/contract/observability/test_failure_trace_visibility.py`
  - `tests/contract/observability/test_runtime_event_publisher.py`
  - `tests/contract/observability/test_span_timing.py`

**Interfaces:**
- Consumes: Task 2 的通用 support 与 invariant marker。
- Produces: 状态型核心机制的聚焦、无节点语义验证。

- [ ] **Step 1: 创建 Context 核心测试**

只保留以下结构化机制：

```text
test_context_window_policy_uses_configured_ratios
test_compaction_replaces_only_covered_history_prefix
test_soft_compaction_failure_keeps_raw_history
test_hard_compaction_failure_blocks_provider_call
test_compaction_preserves_current_native_tool_pair
test_compiled_accounting_matches_provider_request
```

summary、request、observation 全部改成 sentinel；不断言渲染标签、自然语言 summary、具体模型名或真实 tokenizer。

- [ ] **Step 2: 创建 Durable 核心测试**

合并以下通用状态机行为：

```text
test_scheduled_wait_survives_restart_and_resumes_once
test_cancelled_or_expired_wait_never_runs
test_store_migrates_schedule_state
test_subscription_replays_from_cursor
test_subscription_enforces_identity
test_outbox_is_idempotent_across_restart
test_outbox_failure_is_not_task_success
test_changed_evidence_produces_idempotent_resume
test_expired_external_wait_rejects_resume
test_legacy_owner_columns_migrate_to_agent_identity
```

工具固定使用 `probe_tool`，目标和 summary 使用 sentinel。允许 `tmp_path` 下受控 SQLite，不访问外部服务。

- [ ] **Step 3: 创建 Memory 并发边界测试**

仅保留不依赖 Mem0 的三条 queue/lifecycle 行为：

```text
test_runtime_returns_before_background_ingestion_finishes
test_ingestion_serializes_one_identity_and_parallelizes_others
test_queue_close_drains_accepted_work
```

所有 Memory client 使用本地 fake，不导入 `assistant_agent.memory.mem0`。

- [ ] **Step 4: 创建 Observability 核心契约**

合并并改写：

```text
test_assistant_output_accepts_only_text_or_tool_call
test_run_facts_project_to_correlated_events
test_tool_facts_share_span_and_timestamp
test_tool_failure_separates_delivery_and_trace_errors
test_trace_correlation_exists_before_work
test_gateway_timeout_preserves_partial_correlation
test_timeout_audit_keeps_all_correlation_ids
test_started_events_define_span_start_times
```

只断言 canonical event、ID、timestamp、span parent、error code 和结构化 variant，不断言 Langfuse UI payload 或完整错误文案。

- [ ] **Step 5: 运行状态型核心测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/integration/test_context_lifecycle.py \
  tests/core/integration/test_durable_lifecycle.py \
  tests/core/integration/test_memory_lifecycle.py \
  tests/core/contract/test_observability_contract.py
```

Expected: PASS；总用例仍能映射到 `CTX-001`、`DUR-001`、`IDENT-001`、`OBS-001`。

---

### Task 4: 将现有非核心 pytest 迁入独立 incubating 能力域

**Files:**
- Create: `evals/system/incubating/README.md`
- Create directories listed in “Incubating 能力域”
- Move: existing non-core `tests/**/*.py` to `evals/system/incubating/<feature>/checks_*.py`
- Delete after preserved migration: empty `tests/contract/`, `tests/integration/`, `tests/unit/`
- Preserve: `tests/tdd/README.md`、`tests/tdd/conftest.py` 和用户尚未要求删除的临时 feature 目录

**Interfaces:**
- Consumes: Task 2/3 已重新表达的核心不变量。
- Produces: `tests/` 内永久 pytest 只剩 `tests/core`，临时 pytest 只允许在
  `tests/tdd/<feature>/`；现有节点测试内容仍可显式运行和整目录删除。

- [ ] **Step 1: 创建 incubating 总规则与目录模板**

`evals/system/incubating/README.md` 固定模板：

```markdown
# Incubating system checks

这些目录不属于默认 pytest 或发布门禁。每个 feature 目录必须声明：

- Scope
- Mode: offline | real
- Command
- Side effects and gates
- Delete when
- Promote when
```

real check 必须继续要求 real mode、完整配置和 `--allow-*`；offline check 不读取真实 `.env`。

- [ ] **Step 2: 写逐能力 README**

为映射表中的每个目录创建 README。现有 pytest 迁移后的统一显式命令为：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  evals/system/incubating/<feature>/checks_*.py
```

`Delete when` 必须指向具体条件，例如“对应 Task 的真实 Experiment 和正式 system runner 已稳定覆盖该事实”。

- [ ] **Step 3: 迁移当前已修改或未跟踪测试**

优先迁移 `git status --short -- tests` 中的修改、删除替代和未跟踪文件，使用普通文件移动保留工作区内容，
并将文件名从 `test_*.py` 改成 `checks_*.py`。不得用 HEAD 内容覆盖工作区版本。

重点映射：

```text
tests/contract/evals/*                 -> agent-eval-infrastructure/
tests/integration/eval/*               -> agent-eval-infrastructure/
tests/integration/shopping/*           -> shopping/
tests/contract/tools/test_lodging_*     -> lodging/
tests/integration/tools/test_lodging_*  -> lodging/
tests/**/test_mcp_*                     -> mcp/
tests/**/test_image_generation_*        -> image-generation/
tests/integration/server/*              -> server-operations/
tests/integration/observability/*       -> provider-observability/
tests/integration/gateway/*             -> gateway-media-adapter/
```

- [ ] **Step 4: 迁移剩余具体节点和旧实现测试**

```text
email/calendar/contacts/local_file      -> workspace-tools/
tavily/visual_media                     -> web-and-media-tools/
mem0/session snapshot provider details  -> memory-provider/
tokenizer/prompt projection             -> context-features/
hotel/realtime/provider-specific runtime-> runtime-features/
plugin assembly/builtin ownership       -> tool-extension-features/
skill HTTP contract                     -> server-operations/
```

已经由 Task 2/3 重新表达的旧 safety net、Gateway、Tool、Context、Durable、Memory 和 Observability
文件在确认新核心测试通过后移出 `tests/`；若仍有独特实现验证，进入对应 incubating 目录，否则不保留重复副本。

- [ ] **Step 5: 验证没有旁路 pytest**

Run:

```bash
find tests -type f -name '*.py' | sort
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/unit/test_test_policy.py
```

Expected: 第一条命令只显示 `tests/core/**`、`tests/tdd/conftest.py` 和用户保留的
`tests/tdd/<feature>/**`；不存在 TDD 根目录测试或其他旁路 pytest，策略测试 PASS。

- [ ] **Step 6: 验证 incubating 仍可收集**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  --collect-only -q evals/system/incubating/*/checks_*.py
```

Expected: 所有迁移文件可导入和收集；不运行真实 Provider。

---

### Task 5: 重写测试权威、Eval 边界与 Codex skill

**Files:**
- Modify: `AGENTS.md`
- Modify: `tests/README.md`
- Modify: `evals/README.md`
- Modify: `README.md`
- Modify: `.codex/skills/assistant-agent-development-testing/SKILL.md`
- Modify if current authority still references old default suite:
  - `docs/gateway-architecture.md`
  - `docs/runtime-event-stream-architecture.md`
  - `docs/tool-calling-architecture.md`
  - `docs/observability-harness.md`
  - `docs/CONTEXT_ENGINEERING_STATUS.md`
  - `docs/agent-communication-routing.md`

**Interfaces:**
- Consumes: 最终目录与命令。
- Produces: 后续 Codex 可执行、无冲突的测试决策权威。

- [ ] **Step 1: 重写 `tests/README.md`**

将其改为核心测试唯一权威，必须明确：

```text
默认决定：不新增永久 pytest。
只有已登记核心不变量发生变化，或真实框架 bug 证明现有核心安全网缺口时，才修改 tests/core。
具体节点、Provider、Tool、Task、文案和配置变更不得新增核心 pytest；确需 RED/GREEN 时可使用
tests/tdd/<feature> 临时区。
```

写出 invariant marker、目录门禁、文案禁令、60 秒目标、incubating 路由和实际命令。
同时写明 `tests/tdd/<feature>/` 是可手动删除、显式运行、强制 mock/offline 的临时 RED/GREEN 区；
Codex 不得擅自删除，不得自动晋升 core。

- [ ] **Step 2: 重写 development testing skill**

`.codex/skills/assistant-agent-development-testing/SKILL.md` 保持简短，只路由权威并执行以下决策：

```text
1. 读取 tests/README.md 和 tests/core/INVARIANTS.md。
2. 默认不新增测试。
3. 无 core invariant ID 时禁止改 tests/core。
4. 功能开发确需 RED/GREEN 时可使用 tests/tdd/<feature>，但不得默认收集、自动晋升或擅自删除。
5. node-level 长期检查只在有风险证据时进入独立 incubating feature。
6. 禁止文案、prompt、description、console、wrapper 和覆盖率测试。
7. 只运行最小核心集合；发布前或核心基础设施变更才运行裸 pytest。
```

任务汇报增加：

```text
Core invariant: unchanged.
Tests: not added because this is a node-level or implementation-only change.
```

或：

```text
Core invariant: TOOL-001 changed because <stable framework behavior>.
Tests: updated <existing core test>.
```

- [ ] **Step 3: 更新 AGENTS、README 和 evals 权威**

`AGENTS.md` 只保留路由和硬边界：

- `tests/` 的永久/默认 pytest 只包含 core；`tests/tdd/<feature>` 仅是显式运行的临时开发区；
- `evals/system/incubating` 是可删除的节点专项区；
- 不得为小功能机械加测试。

`evals/README.md` 保留当前未提交的 shopping、四分 Score 和 Agent Task 内容，在开头边界处加入
incubating，不覆盖这些已有改动。

`README.md` 的 basic check 继续使用裸 pytest，但解释它只运行核心框架安全网。

- [ ] **Step 4: 修复当前架构权威引用**

只修改 `docs/*.md` 当前权威中明确引用旧目录、旧默认全量 pytest 或“每个节点补 pytest”的文字。
历史 `docs/development/**` 和 `docs/superpowers/**` 不批量改写。

- [ ] **Step 5: 验证 skill 和文档**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  /home/lenovo1/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .codex/skills/assistant-agent-development-testing

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root .
```

Expected: skill validation PASS；collector 无新增断链。collector 输出只用于审计，不写文件。

---

### Task 6: 完成核心套件验证与迁移审计

**Files:**
- Verify: all files changed by Tasks 1-5
- Do not modify: unrelated dirty source/eval/config files

**Interfaces:**
- Consumes: 完整核心套件、incubating 迁移和文档规则。
- Produces: 可复核的最终测试结果、耗时、迁移清单和限制。

- [ ] **Step 1: 检查默认收集**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest --collect-only -q
```

Expected: 只收集 `tests/core/**`，每个 item 都有已登记 invariant ID。
即使存在用户保留的 `tests/tdd/<feature>`，裸 collect 也不得包含它。

- [ ] **Step 2: 运行核心 pytest**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
```

Expected: PASS，正常机器目标耗时不超过 60 秒，不调用真实 Provider。

- [ ] **Step 3: 检查目录和文案门禁**

Run:

```bash
find tests -type f -name '*.py' | sort
rg -n 'assistant_agent\\.tools\\.plugins\\.builtin|evals\\.agent' tests/core
rg -n 'assert .*[\u4e00-\u9fff]' tests/core --glob '*.py'
```

Expected: 永久 Python 测试只在 core；若用户保留临时 TDD，则 Python 测试还可位于
`tests/tdd/<feature>/**`，并允许 `tests/tdd/conftest.py`。不得位于 `tests/tdd/` 根目录或其他
`tests/` 子树；后两条没有 core 违规结果。

- [ ] **Step 4: 检查工作区完整性**

Run:

```bash
git status --short
git diff --name-status
git diff --check
```

Expected: 不相关源码、eval 和配置改动仍在；没有 whitespace error；测试迁移目标明确。

- [ ] **Step 5: 最终报告**

报告必须包含：

- 核心 invariant 与用例数量；
- 裸 pytest 命令、结果和耗时；
- incubating 目录及迁入来源；
- 保留的临时 TDD feature 目录，或确认当前无临时 feature；
- 删除或未迁移的测试；
- 未运行任何真实 Provider；
- 当前工作区已有并行改动造成的限制；
- `Tests: updated core framework suite because the default test boundary changed.`

本轮不自动提交；如用户要求提交，再只选择本任务文件并单独处理与其他改动重叠的文件。
