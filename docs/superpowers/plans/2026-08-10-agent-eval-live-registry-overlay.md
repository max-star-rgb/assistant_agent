# Agent Eval 生产 Registry 与精确替换实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让正式 Langfuse Agent Experiment 默认复用完整生产 Tool Registry 和真实读写依赖，同时允许 Task 在不改变 ToolSpec 的前提下精确替换少量既有 Tool，并消除高德 MCP 重复注册。

**Architecture:** `AgentGraphRuntime` 继续负责生产依赖与默认 Registry 的唯一装配，新增 seal 前 `registry_transform` 扩展点；Agent eval 通过该扩展点应用默认为空的 replacement overlay。Environment 的离线静态校验与正式运行时 Registry 校验分离，Evidence 记录每次 Tool 调用的 `live`/`controlled_replacement` provenance。

**Tech Stack:** Python 3.12、Pydantic、AgentGraphRuntime、ToolRegistry、pytest、Langfuse Experiment、MCP、SQLite Workflow Store。

## Global Constraints

- 默认 Python 使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest、`--inspect` 和 calibration 必须保持 mock/local/offline，不访问真实 Provider、MCP 或外部服务。
- 正式 `--run` 使用 `MULTIMODAL_AGENT_PROVIDER_MODE=real`；服务以 real 模式启动即代表 operator 已授权已配置 Tool 的真实读写，Langfuse UI 不增加第二个确认字段。
- 生产 Registry 是 Tool names、ToolSpec、Plugin/MCP allowlist、readiness 和权限的唯一事实源；replacement 只能替换生产 Registry 中已存在的名称。
- replacement 必须保持模型可见 ToolSpec 完全一致，只能改变执行依赖；禁止追加同名 Tool 或静默 mock fallback。
- 所有显式 Tool 调用继续经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- 不按 Task capability、用户话术或 grader 裁剪目录；结构化 `tool_visibility` 只能收窄最终完整目录。
- 不读取、写入或提交凭据、真实 Provider 原始响应、真实用户数据或评测 artifact。
- 不回滚当前 worktree 中与本任务无关的用户改动；修改已有脏文件前先检查 diff 并做最小补丁。
- Core invariant 默认为 unchanged；本功能测试放入可手动删除的 `tests/tdd/agent-eval-live-registry-overlay/`，不新增永久 core pytest。

---

### Task 1: Registry overlay 契约与原子装配

**Files:**
- Create: `evals/agent/registry_overlay.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_registry_overlay.py`

**Interfaces:**
- Produces: `EvalToolReplacement`、`EvalToolProvenance`、`EvalRegistryAssembly`。
- Produces: `apply_tool_replacements(production_registry: ToolRegistry, replacements: Iterable[EvalToolReplacement]) -> EvalRegistryAssembly`。

- [ ] **Step 1: 写 overlay RED 测试**

覆盖空 replacement、同名精确替换、未知名称、重复声明、空 reason/source、名称不一致、ToolSpec 不一致和原子失败：

```python
def test_exact_replacement_preserves_catalog_and_changes_implementation():
    production = sealed_registry(ProbeTool(result="live"))
    controlled = ProbeTool(result="controlled")
    assembly = apply_tool_replacements(
        production,
        [EvalToolReplacement(
            tool_name="probe",
            tool=controlled,
            reason="deterministic provider failure",
            source_ref="tests:probe",
        )],
    )
    assert assembly.registry.list() == ["probe"]
    assert assembly.registry.get_spec("probe") == production.get_spec("probe")
    assert assembly.registry.get("probe") is controlled
    assert assembly.provenance["probe"].dependency_mode == "controlled_replacement"
```

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_registry_overlay.py
```

预期：`evals.agent.registry_overlay` 尚不存在。

- [ ] **Step 3: 实现 overlay 类型与函数**

```python
DependencyMode = Literal["live", "controlled_replacement"]

@dataclass(frozen=True)
class EvalToolReplacement:
    tool_name: str
    tool: Tool
    reason: str
    source_ref: str

class EvalToolProvenance(BaseModel):
    dependency_mode: DependencyMode
    production_source_type: str
    production_source_ref: str
    replacement_source_ref: str | None = None
    replacement_reason: str | None = None

@dataclass(frozen=True)
class EvalRegistryAssembly:
    registry: ToolRegistry
    provenance: dict[str, EvalToolProvenance]
```

先 materialize 并完整校验 replacement，再构造和 seal 新 Registry。复用生产 registration record；eval provenance 独立保存，不扩展通用 `ToolRegistrationRecord.source_type`。

- [ ] **Step 4: 运行测试和格式检查**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_registry_overlay.py
git diff --check -- evals/agent/registry_overlay.py \
  tests/tdd/agent-eval-live-registry-overlay/test_registry_overlay.py
```

- [ ] **Step 5: 创建检查点提交**

```bash
git add evals/agent/registry_overlay.py \
  tests/tdd/agent-eval-live-registry-overlay/test_registry_overlay.py
git commit -m "feat(eval): add atomic tool replacement overlay"
```

### Task 2: Runtime 生产装配扩展点

**Files:**
- Modify: `src/assistant_agent/runtime/runtime.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_runtime_registry_transform.py`

**Interfaces:**
- Produces: `RegistryTransform = Callable[[ToolRegistry], ToolRegistry]`。
- Extends: 在现有 `AgentGraphRuntime.__init__` 参数末尾增加 `registry_transform: RegistryTransform | None = None`。
- Invariant: `registry` 与 `registry_transform` 互斥；transform 只在生产 Registry 创建后、ToolExecutor 创建前执行一次。

- [ ] **Step 1: 写 Runtime transform RED 测试**

```python
def test_runtime_transforms_default_registry_before_executor_binding():
    seen = []
    def transform(production: ToolRegistry) -> ToolRegistry:
        seen.append(production)
        return clone_with_probe_replacement(production)
    runtime = AgentGraphRuntime(
        config=ProviderConfig(provider_mode="mock"),
        chat_adapter=ScriptedChatAdapter(),
        registry_transform=transform,
    )
    try:
        assert len(seen) == 1
        assert runtime.tool_executor.registry is runtime.registry
    finally:
        runtime.close()
```

另测同时传 `registry`/`registry_transform`、返回非 Registry、返回未 seal Registry时 fail closed。

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_runtime_registry_transform.py
```

- [ ] **Step 3: 应用 transform 并复用现有绑定**

```python
if registry is not None and registry_transform is not None:
    raise ValueError("registry and registry_transform are mutually exclusive")
production_registry = create_default_registry(
    self.config,
    video_context_store=self.video_context_store,
    realtime_video_memory_store=self.realtime_video_memory_store,
    embedding_coordinator_store=self.embedding_coordinator_store,
    visual_semantic_store_pool=self.visual_semantic_store_pool,
    visual_reminder_registry=self.visual_reminder_registry,
    visual_memory_text_index=self.visual_memory_text_index,
    durable_task_service=self.durable_task_service,
    workflow_service=self.workflow_service,
)
self.registry = (
    registry_transform(production_registry)
    if registry_transform is not None
    else production_registry
)
```

transform 后继续执行 durable task binding、workflow Tool 检查、visual dependency binding 和 `ToolExecutor(registry=self.registry)`；禁止从 `src/assistant_agent/` import eval 模块。

- [ ] **Step 4: 运行 Runtime 定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_runtime_registry_transform.py \
  tests/core/integration/test_runtime_lifecycle.py
```

- [ ] **Step 5: 创建检查点提交**

```bash
git add src/assistant_agent/runtime/runtime.py \
  tests/tdd/agent-eval-live-registry-overlay/test_runtime_registry_transform.py
git commit -m "feat(runtime): allow sealed registry transformation"
```

### Task 3: Environment 双阶段校验、正式 Runtime 与 Evidence provenance

**Files:**
- Modify: `evals/agent/contracts.py`
- Modify: `evals/agent/environment_base.py`
- Modify: `evals/agent/task_support.py`
- Modify: `evals/agent/evidence.py`
- Modify: `evals/agent/grading.py`
- Modify: `evals/agent/langfuse_backend.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_environment_runtime_assembly.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_evidence_provenance.py`

**Interfaces:**
- Produces: `ControlledTaskEnvironment.tool_replacements(production_registry: ToolRegistry) -> tuple[EvalToolReplacement, ...]`，默认空。
- Produces: `validate_runtime_registry(assembly: EvalRegistryAssembly) -> EnvironmentValidation`。
- Extends: `ToolExecution.dependency_mode`、`production_source_ref`、`replacement_source_ref`，均有兼容默认值。
- Replaces formal path: `execute_isolated_runtime()` 改为不覆盖生产业务配置的 `execute_agent_eval_runtime()`。

- [ ] **Step 1: 写 Environment/Evidence RED 测试**

证明无 config 的 `validate()`/`--inspect` 不创建真实 Registry；正式 execute 先创建生产 Registry，再把它传给 replacement hook 并应用 overlay；runtime validation 在 chat 前失败；Evidence 区分 live/replacement；旧 calibration 缺新字段仍可加载。

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_environment_runtime_assembly.py \
  tests/tdd/agent-eval-live-registry-overlay/test_evidence_provenance.py
```

- [ ] **Step 3: 实现静态校验与 runtime 校验**

`validate()` 只校验 required sets、visibility shape 和不依赖 live Registry 的 Task Rule provenance；`validate_runtime_registry()` 校验 replacement reason/source、生产 Registry seal、replacement 子集/ToolSpec、可见目录与 outcome contract。

```python
def tool_replacements(
    self,
    production_registry: ToolRegistry,
) -> tuple[EvalToolReplacement, ...]:
    del production_registry
    return ()

def _registry_transform(self, production: ToolRegistry) -> ToolRegistry:
    replacements = self.tool_replacements(production)
    assembly = apply_tool_replacements(production, replacements)
    self._runtime_assembly = assembly
    self.validate_runtime_registry(assembly).require_valid()
    return assembly.registry
```

`tool_outcome_expectations(available_tools)` 仅根据 Evidence names 和 required sets 构造结果，不为离线 grading 创建完整 Registry。

- [ ] **Step 4: 正式路径保留生产配置**

```python
runtime = AgentGraphRuntime(
    config=resolved_config,
    registry_transform=registry_transform,
    chat_adapter=chat_adapter,
    trace_store=InMemoryTraceStore(),
    **dict(runtime_overrides or {}),
)
```

只保留 Evidence 所需 `InMemoryTraceStore`；删除对 mem0、history、checkpointer、durable flags 和业务 store 的统一 `replace()`。mock 测试通过显式 mock config/adapter 保持离线。

- [ ] **Step 5: 增加向后兼容 provenance**

```python
class ToolExecution(BaseModel):
    dependency_mode: Literal["live", "controlled_replacement"] = "live"
    production_source_ref: str | None = None
    replacement_source_ref: str | None = None
```

在现有 `ToolExecution` 字段后增加上述三个字段。`tool_executions(events, provenance=assembly.provenance)` 按 tool name 投影字段；`RunEvidence.schema_version` 暂不升级，旧 calibration 通过默认值继续加载。

- [ ] **Step 6: 运行定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay \
  tests/tdd/eval-convergence/test_native_experiment_scoring.py \
  tests/tdd/eval-convergence/test_native_calibration.py
```

- [ ] **Step 7: 创建检查点提交**

```bash
git add evals/agent/contracts.py evals/agent/environment_base.py \
  evals/agent/task_support.py evals/agent/evidence.py evals/agent/grading.py \
  evals/agent/langfuse_backend.py \
  tests/tdd/agent-eval-live-registry-overlay
git commit -m "feat(eval): run environments on production registry"
```

### Task 4: Deep Research 使用生产 Workflow 状态

**Files:**
- Modify: `evals/agent/deep_research_support.py`
- Modify: `tests/tdd/eval-convergence/test_deep_research_environment.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_deep_research_live_registry.py`

**Interfaces:**
- Consumes: 默认空 replacement 和 `AgentGraphRuntime.workflow_service`。
- Produces: `final_state_reader()` 从 Runtime-owned Workflow store 按本次 `workflow_id` 读取终态。

- [ ] **Step 1: 写 Deep Research RED 测试**

断言 Environment 不创建 `TemporaryDirectory`/`InMemoryWorkflowStore`、不调用 `build_controlled_registry()`、replacement 为空；production-shaped AMap 中最终只有一个 `maps_geo`；`workflow_submit` 与 state reader 使用同一个 Runtime service。

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_deep_research_live_registry.py \
  tests/tdd/eval-convergence/test_deep_research_environment.py
```

- [ ] **Step 3: 删除 Mission 私有 Registry/Store 装配**

```python
class DeepResearchMissionEnvironment(ControlledTaskEnvironment):
    dependency_label = "live:production-workflow"
    writes = True
    state_reset = "persistent_production_store"

    def tool_replacements(
        self,
        production_registry: ToolRegistry,
    ) -> tuple[EvalToolReplacement, ...]:
        del production_registry
        return ()
```

删除临时路径和当前返回 `{"workflow_service": self.workflow_service}` 的 `runtime_overrides()`。`final_state_reader` 使用 `runtime.workflow_service.store`；生产未暴露 `workflow_submit` 或 store 不可读时在 chat 前作为 infrastructure failure。

- [ ] **Step 4: 更新 scripted 离线 fixture**

pytest 显式注入 mock production-shaped WorkflowService；该注入仅存在于测试，不回到正式 Environment。

- [ ] **Step 5: 运行 Deep Research 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_deep_research_live_registry.py \
  tests/tdd/eval-convergence/test_deep_research_environment.py
```

- [ ] **Step 6: 创建检查点提交**

```bash
git add evals/agent/deep_research_support.py \
  tests/tdd/eval-convergence/test_deep_research_environment.py \
  tests/tdd/agent-eval-live-registry-overlay/test_deep_research_live_registry.py
git commit -m "fix(eval): use live workflow registry for deep research"
```

### Task 5: AMap、旅行与会议 Task 改为精确 replacement

**Files:**
- Modify: `evals/agent/travel_support.py`
- Modify: `evals/agent/tasks/amap_weather_forecast_date_grounding/environment.py`
- Modify: `evals/agent/tasks/amap_weather_missing_city_clarification/environment.py`
- Modify: `evals/agent/tasks/amap_weather_provider_failure_recovery/environment.py`
- Modify: `evals/agent/tasks/travel_city_poi_disambiguation/environment.py`
- Modify: `evals/agent/tasks/travel_transit_route_evidence_chain/environment.py`
- Modify: `evals/agent/tasks/travel_itinerary_planning/environment.py`
- Modify: `evals/agent/missions/meeting_logistics_tentative_calendar_commit/environment.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_amap_task_replacements.py`

**Interfaces:**
- Produces: `amap_tool_replacements(production_registry, definitions, runner) -> tuple[EvalToolReplacement, ...]`。
- Removes from formal path: `add_controlled_amap_tools()`/`build_travel_registry()`。

- [ ] **Step 1: 写 AMap RED 测试**

只替换目标 Tool；其他 AMap Tool 保持 live；生产缺目标时 fail closed；names 无重复；weather timeout 的 ToolSpec 与 production 完全一致。

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_amap_task_replacements.py
```

- [ ] **Step 3: 实现契约保持型 AMap replacement**

复用生产 MCP proxy 的名称、input model 和 ToolSpec，只把 `run()` 委托给 Task runner；不得用 deployment allowlist 补生产目录中缺失的 Tool。

- [ ] **Step 4: 迁移八个 Environment**

删除各自 `build_registry()`，保留 fixture、required outcomes、state reader 和 Task Rule。会议 Mission 的 calendar fixture 同样改成精确 replacement。

- [ ] **Step 5: 运行测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_amap_task_replacements.py \
  tests/tdd/eval-convergence/test_native_experiment_scoring.py
```

- [ ] **Step 6: 创建检查点提交**

```bash
git add evals/agent/travel_support.py \
  evals/agent/tasks/amap_weather_* \
  evals/agent/tasks/travel_city_poi_disambiguation \
  evals/agent/tasks/travel_transit_route_evidence_chain \
  evals/agent/tasks/travel_itinerary_planning \
  evals/agent/missions/meeting_logistics_tentative_calendar_commit \
  tests/tdd/agent-eval-live-registry-overlay/test_amap_task_replacements.py
git commit -m "refactor(eval): replace selected amap tools exactly"
```

### Task 6: 其余受控 Task 迁移

**Files:**
- Modify: `evals/agent/batch_cases.py`
- Modify: `evals/agent/tasks/file_conflicting_receipts_resolution/environment.py`
- Modify: `evals/agent/tasks/file_missing_receipt_clarification/environment.py`
- Modify: `evals/agent/tasks/email_file_booking_amount_reconciliation/environment.py`
- Modify: `evals/agent/tasks/travel_lodging_constraint_grounding/environment.py`
- Modify: `evals/agent/tasks/travel_skill_proactive_loading/environment.py`
- Modify: `evals/agent/tasks/visual_memory_last_seen_object/environment.py`
- Modify: `evals/agent/tasks/visual_memory_not_found_honesty/environment.py`
- Modify: `evals/agent/tasks/website_unverified_url_honesty/environment.py`
- Modify: `evals/agent/task_support.py`
- Create: `tests/tdd/agent-eval-live-registry-overlay/test_task_replacement_inventory.py`

**Interfaces:**
- Consumes: `EvalToolReplacement` hook。
- Removes from formal path: `build_controlled_base_registry()`、`build_controlled_registry()`。

- [ ] **Step 1: 写 Task inventory RED 测试**

用 mock production-shaped Registry 遍历所有 Git Task/Mission，断言不覆盖 `build_registry()`、replacement names 唯一且有 reason/source；Deep Research 为空；确定性文件、邮件、日历、住宿、视觉记忆和 website Task 只替换 fixture 目标 Tool。

- [ ] **Step 2: 运行 RED 测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay/test_task_replacement_inventory.py
```

- [ ] **Step 3: 迁移 batch 与独立 Environment**

把现有 replacement dict 转成 `EvalToolReplacement` tuple。保留临时文件、固定 email backend、局部 SQLite calendar 等确定性 fixture；未列入 replacement 的生产 Tool 全部保持 live。

Website Task 必须以生产 `web_page_inspect`/`web_page_explore` 为原始契约再替换 backend；生产未启用 website guidance 时正式运行在 chat 前失败，不能自行启用 mock Plugin 补目录。

- [ ] **Step 4: 删除平行完整目录 builder**

```bash
rg -n "build_controlled_registry|build_controlled_base_registry|add_controlled_amap_tools" \
  evals/agent
```

预期：正式 Task/Mission Environment 无命中；无兼容调用后删除 builder 和 `_FallbackAmapRunner`。

- [ ] **Step 5: 运行 feature 测试与 inspect**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py \
  --inspect --suite deep_research
```

- [ ] **Step 6: 创建检查点提交**

```bash
git add evals/agent/batch_cases.py evals/agent/task_support.py \
  evals/agent/tasks/file_conflicting_receipts_resolution \
  evals/agent/tasks/file_missing_receipt_clarification \
  evals/agent/tasks/email_file_booking_amount_reconciliation \
  evals/agent/tasks/travel_lodging_constraint_grounding \
  evals/agent/tasks/travel_skill_proactive_loading \
  evals/agent/tasks/visual_memory_last_seen_object \
  evals/agent/tasks/visual_memory_not_found_honesty \
  evals/agent/tasks/website_unverified_url_honesty \
  tests/tdd/agent-eval-live-registry-overlay/test_task_replacement_inventory.py
git commit -m "refactor(eval): migrate tasks to production registry overlay"
```

### Task 7: 权威文档同步

**Files:**
- Modify: `evals/README.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`
- Modify: `scripts/README.md`

**Interfaces:**
- `evals/README.md` 唯一展开 live Registry、replacement、真实副作用、失败归因和运行顺序。
- 其他文档只记录通用架构或入口，不复制 webhook 操作契约。

- [ ] **Step 1: 修改权威正文**

把“默认完整受控目录、不连接真实高德”改为“正式 `--run` 默认完整生产目录；Task 可精确 replacement；inspect/pytest/calibration 离线”。明确 real 服务启动授权和 UI 不扩权。

- [ ] **Step 2: 更新 Skill 和脚本索引**

Skill 要求 Environment 声明 live/frozen/simulated dependency，并验证 replacement 是生产 Registry 子集。脚本文档只增加真实副作用提示与权威链接。

- [ ] **Step 3: 运行文档证据检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root .
rg -n \
  'assistant-agent-eval-webhook|REMOTE_EXPERIMENT_SIGNING_SECRET|Default config|projectId|datasetId' \
  evals/README.md docs/observability-harness.md scripts/README.md
```

- [ ] **Step 4: 创建检查点提交**

```bash
git add evals/README.md docs/tool-calling-architecture.md \
  .codex/skills/langfuse-eval-engineering/SKILL.md scripts/README.md
git commit -m "docs(eval): define live registry overlay semantics"
```

### Task 8: 最终离线验证与真实烟测交接

**Files:**
- Verify only: all task-related files above
- Never commit: `.data/**`、Langfuse receipt/log、真实 Workflow 数据、Provider/MCP 输出

**Interfaces:**
- Real smoke target: `deep_research_autonomous_admission`，再运行一个 replacement 故障恢复 Task。

- [ ] **Step 1: 运行最小完整离线验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/agent-eval-live-registry-overlay \
  tests/tdd/eval-convergence/test_deep_research_environment.py \
  tests/tdd/eval-convergence/test_native_experiment_scoring.py \
  tests/tdd/eval-convergence/test_native_calibration.py
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_agent_evals.py \
  --inspect --task deep_research_autonomous_admission
```

- [ ] **Step 2: 检查 diff 与测试政策**

```bash
git diff --check
git status --short
git diff --stat
```

汇报：

```text
Core invariant: unchanged.
Tests: added tests/tdd/agent-eval-live-registry-overlay for temporary RED/GREEN; user may delete the directory manually.
```

- [ ] **Step 3: 用户确认本轮真实验证后运行单 Item**

real Assistant Server 已按生产配置启动后，从 Langfuse UI 运行：

```json
{"task":"deep_research_autonomous_admission","runName":"deep-research-live-registry-smoke"}
```

确认 `mcp.amap_maps.maps_geo` 只有一个 live registration、真实 `workflow_submit` 成功、Workflow 终态可读，且三个 task-level Score 已按 trace/observation 落库。

- [ ] **Step 4: 运行 replacement 烟测并报告副作用**

运行 `amap_weather_provider_failure_recovery`，确认只有 `mcp.amap_maps.maps_weather` 是 `controlled_replacement`，其他配置 Tool 均为 live。报告真实外部调用、写入资源、Trace、Score 和未清理副作用，不保存原始 Provider payload。

- [ ] **Step 5: 最终提交检查**

```bash
git diff --cached --name-only
```

确认提交中没有用户既有无关改动、凭据、`.data/**` 或运行日志；若前述任务已分别提交，本步骤不再创建重复总提交。
