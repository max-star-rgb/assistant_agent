# Agent Eval Task 收敛重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Agent eval 的 Environment 生命周期、grader 调用壳和 Calibration 版本读取集中到共享框架，避免公共协议变化横扫全部 Task。

**Architecture:** 新增 `ControlledTaskEnvironment` 模板统一 `describe/validate/outcomes/execute`，Task 子类仅保留受控依赖、registry、状态和专属 Rule hooks。`grader_for_response_quality()` 固定三项 Judge 编排；Calibration 通过单一版本分派入口读取并规范化。

**Tech Stack:** Python 3.11、Pydantic、pytest、现有 `AgentGraphRuntime` 与 Tool Registry。

## Global Constraints

- 保持现有 Task 请求、Task ID、Suite、工具行为、Evidence、四项独立 Score 和 Langfuse 契约不变。
- 默认 Environment 暴露共享完整 Agent eval 工具目录；结构化 visibility override 只能收窄已注册工具。
- pytest 仅使用 mock/local/offline，不调用真实 Provider、MCP、Langfuse 或外部服务。
- `task.json` 和 `calibration.json` 内容不迁移。
- 不修改或回滚工作区中与本任务无关的现有改动。

---

### Task 1: 建立共享 Environment 模板

**Files:**
- Create: `evals/agent/environment_base.py`
- Create: `tests/integration/eval/test_agent_eval_environment_base.py`
- Modify: `evals/agent/task_support.py`

**Interfaces:**
- Consumes: `build_controlled_registry()`、`outcome_expectations()`、`execute_isolated_runtime()`。
- Produces: `EnvironmentToolVisibility`、`ControlledTaskEnvironment` 及稳定 hooks。

- [ ] **Step 1: 写失败测试**

新增测试子类：

```python
class _ExampleEnvironment(ControlledTaskEnvironment):
    dependency_label = "controlled:test-example"

    def build_registry(self) -> ToolRegistry:
        return build_controlled_registry()

    def required_successes(self) -> tuple[str, ...]:
        return ("email_search",)

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> Mapping[str, AssertionResult]:
        return {
            "fixture_ready": rule_assertion(
                "email_search" in registry.list(),
                "email_search registered",
                label="测试依赖可用",
            )
        }
```

断言：

- `describe()` 使用 registry 的真实数量；
- `validate()` 检查 registry、outcome 覆盖和 Task Rule；
- 默认 expectations 覆盖完整 registry；
- Evidence 子集仍强制保留 required 工具；
- visibility override 精确写入请求 metadata；
- unknown allowlist 工具使 validation 失败。

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_environment_base.py
```

Expected: collection FAIL，`evals.agent.environment_base` 尚不存在。

- [ ] **Step 3: 实现最小共享模板**

实现：

```python
@dataclass(frozen=True)
class EnvironmentToolVisibility:
    profile: str
    allowed_tools: tuple[str, ...]


class ControlledTaskEnvironment:
    dependency_label = "controlled:task"
    writes = False
    state_reset = "per_task_run"
    tool_catalog_label = "default_complete_agent_eval_registry"

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None: ...

    def setup(self) -> None: ...
    def build_registry(self) -> ToolRegistry: ...
    def required_successes(self) -> tuple[str, ...]: ...
    def required_failures(self) -> Mapping[str, str]: ...
    def visibility_override(self) -> EnvironmentToolVisibility | None: ...
    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> Mapping[str, AssertionResult]: ...
    def initial_state(self, request: UserRequest) -> dict[str, Any]: ...
    def before_run(self, runtime: AgentGraphRuntime, request: UserRequest) -> None: ...
    def final_state_reader(self, request: UserRequest) -> StateReader | None: ...
    def runtime_overrides(self, request: UserRequest) -> Mapping[str, Any]: ...
```

`describe/validate/tool_outcome_expectations/execute` 为 final-style 公共流程；registry 惰性缓存。把 registry 子集复制提取为 `subset_registry()`，由共享模板统一调用。

- [ ] **Step 4: 运行测试并确认 GREEN**

Run Task 1 的测试文件，Expected: PASS。

---

### Task 2: 迁移批量 Task Environment

**Files:**
- Modify: `evals/agent/batch_cases.py`
- Modify: `tests/integration/eval/test_agent_eval_task.py`

**Interfaces:**
- Consumes: `ControlledTaskEnvironment` hooks。
- Produces: 8 个现有 `environment_type()` wrapper 行为不变，但公共生命周期由基类拥有。

- [ ] **Step 1: 写失败契约**

在 `test_agent_eval_task.py` 断言所有由 `environment_type()` 生成的 Environment：

```python
assert isinstance(environment, ControlledTaskEnvironment)
assert "describe" not in BatchCaseEnvironment.__dict__
assert "validate" not in BatchCaseEnvironment.__dict__
assert "tool_outcome_expectations" not in BatchCaseEnvironment.__dict__
assert "execute" not in BatchCaseEnvironment.__dict__
```

- [ ] **Step 2: 运行定向测试并确认 RED**

Expected: FAIL，`BatchCaseEnvironment` 仍自己实现四个生命周期方法。

- [ ] **Step 3: 迁移为 hooks**

让 `BatchCaseEnvironment(ControlledTaskEnvironment)`：

- `setup()` 创建临时目录、calendar adapter 和文件；
- `dependency_label` 由 property 返回 `controlled:<case_id>`；
- `required_successes()` 从 `REQUIRED_TOOLS` 返回当前 case 工具；
- `task_validation_checks()` 保留目标依赖与隔离检查；
- `build_registry()` 保留现有 Task-specific replacements；
- `initial_state()`、`before_run()`、`final_state_reader()`、
  `runtime_overrides()` 保留 calendar/memory 行为。

删除本类的 `describe/validate/tool_outcome_expectations/execute`。

- [ ] **Step 4: 运行批量 Task 定向测试并确认 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_task.py
```

Expected: PASS。

---

### Task 3: 迁移文件与邮件自定义 Environment

**Files:**
- Modify: `evals/agent/tasks/file_conflicting_receipts_resolution/environment.py`
- Modify: `evals/agent/tasks/file_missing_receipt_clarification/environment.py`
- Modify: `evals/agent/tasks/email_file_booking_amount_reconciliation/environment.py`
- Modify: `tests/integration/eval/test_file_conflicting_receipts_agent_eval_task.py`
- Modify: `tests/integration/eval/test_file_missing_receipt_agent_eval_task.py`
- Modify: `tests/integration/eval/test_email_file_reconciliation_agent_eval_task.py`

**Interfaces:**
- Consumes: `ControlledTaskEnvironment`。
- Produces: 三个 Task 仅实现 setup、registry、required tools 和专属 validation hooks。

- [ ] **Step 1: 写结构契约并确认 RED**

分别断言三个类继承共享模板，且类字典不再定义四个公共生命周期方法。

- [ ] **Step 2: 迁移 `file_conflicting_receipts_resolution`**

- `setup()` 创建三份冻结文件；
- `build_registry()` 只替换 `file_read`；
- `required_successes()` 返回 `("file_read",)`；
- `task_validation_checks()` 验证文件内容和隔离目录。

- [ ] **Step 3: 迁移 `file_missing_receipt_clarification`**

- `setup()` 创建缺少一份凭证的冻结目录；
- registry/required tools 保持现状；
- Task checks 保留缺失材料 oracle 与隔离检查。

- [ ] **Step 4: 迁移 `email_file_booking_amount_reconciliation`**

- `setup()` 创建冻结邮件 backend 和行程文件；
- registry 替换 `email_search/email_read/file_read`；
- required successes 保持三工具；
- Task checks 保留邮件与文件内容验证。

- [ ] **Step 5: 运行三个 Task 测试并确认 GREEN**

运行上述三个测试文件，Expected: PASS。

---

### Task 4: 迁移天气与旅行自定义 Environment

**Files:**
- Modify: `evals/agent/tasks/amap_weather_forecast_date_grounding/environment.py`
- Modify: `evals/agent/tasks/amap_weather_missing_city_clarification/environment.py`
- Modify: `evals/agent/tasks/amap_weather_provider_failure_recovery/environment.py`
- Modify: `evals/agent/tasks/travel_city_poi_disambiguation/environment.py`
- Modify: `evals/agent/tasks/travel_lodging_constraint_grounding/environment.py`
- Modify: `evals/agent/tasks/travel_transit_route_evidence_chain/environment.py`
- Modify: `tests/integration/eval/test_amap_weather_agent_eval_tasks.py`
- Modify: `tests/integration/eval/test_travel_foundation_agent_eval_tasks.py`

**Interfaces:**
- Consumes: `ControlledTaskEnvironment`、现有 `build_travel_registry()` 和受控 runner。
- Produces: 六个 Task 只保留 runner/oracle 和 Task hooks。

- [ ] **Step 1: 写结构契约并确认 RED**

参数化断言六个类继承共享模板且不定义公共生命周期方法。

- [ ] **Step 2: 迁移三个天气 Environment**

每个类：

- `build_registry()` 继续把目标 weather definition 路由到当前 runner；
- `required_successes()` 或 `required_failures()` 保持当前成功/超时语义；
- missing-city Task 的 required successes 为空；
- Task checks 保留 schema、冻结日期、provider failure 和隔离事实。

- [ ] **Step 3: 迁移三个旅行 Environment**

- POI Task 保留 text-search runner 与 required success；
- lodging Task 保留冻结 lodging adapter 与 required success；
- transit Task 保留 geo + transit runner 与两个 required successes；
- Task checks 保留城市消歧、预算约束和路线 evidence oracle。

- [ ] **Step 4: 运行天气与旅行测试并确认 GREEN**

运行两个测试文件，Expected: PASS。

---

### Task 5: 收敛 Task grader

**Files:**
- Modify: `evals/agent/batch_grading.py`
- Modify: `evals/agent/tasks/*/grader.py`
- Modify: `tests/integration/eval/test_agent_eval_task.py`

**Interfaces:**
- Consumes: `grade_case(evidence, judge, response_quality_rubric=...)`。
- Produces: `grader_for_response_quality(rubric) -> Callable[[RunEvidence, LLMJudge], TaskJudgeResult]`。

- [ ] **Step 1: 写失败测试**

测试 factory 返回 callable，并遍历全部 Task grader：

```python
grader = load_entrypoint(task.grader)
assert getattr(grader, "response_quality_rubric") == module.RESPONSE_QUALITY_RUBRIC
```

同时使用记录 Judge 验证 criterion 顺序仍为
`tool_semantics/grounding/response_quality`。

- [ ] **Step 2: 运行测试并确认 RED**

Expected: FAIL，factory 和 rubric metadata 尚不存在。

- [ ] **Step 3: 实现 factory 并迁移 17 个 grader**

实现 closure，并通过 `setattr()` 暴露只读调试 metadata：

```python
def grader_for_response_quality(rubric: str) -> TaskGrader:
    normalized = rubric.strip()

    def grade(evidence: RunEvidence, judge: LLMJudge) -> TaskJudgeResult:
        return grade_case(
            evidence,
            judge,
            response_quality_rubric=normalized,
        )

    grade.response_quality_rubric = normalized
    return grade
```

每个 Task 删除手写 `grade()`，改为：

```python
grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
```

- [ ] **Step 4: 运行 grader 契约并确认 GREEN**

Run `tests/integration/eval/test_agent_eval_task.py`，Expected: PASS。

---

### Task 6: 增加 Calibration 版本分派

**Files:**
- Modify: `evals/agent/calibration.py`
- Modify: `tests/integration/eval/test_agent_eval_task.py`

**Interfaces:**
- Consumes: `calibration_path(task_id)` 和现有 `CalibrationSet` v3。
- Produces: `load_calibration_set(task_id) -> CalibrationSet`。

- [ ] **Step 1: 写失败测试**

断言两个读取入口都调用公开 loader，并验证：

- 当前 17 个 calibration 文件都加载为 v3；
- 未知 `schema_version` 报告包含版本号的 `ValueError`；
- fixture 内容不会被 loader 改写。

- [ ] **Step 2: 运行测试并确认 RED**

Expected: FAIL，`load_calibration_set()` 尚不存在。

- [ ] **Step 3: 实现版本分派**

读取 JSON object，检查字符串 `schema_version`，通过版本表：

```python
CALIBRATION_READERS = {
    "agent_eval_calibration_v3": CalibrationSet.model_validate,
}
```

规范化为 `CalibrationSet`。`run_calibration()` 和
`load_labeled_calibration_judge()` 只调用该入口。

- [ ] **Step 4: 运行 Calibration 契约并确认 GREEN**

Run `tests/integration/eval/test_agent_eval_task.py` 中 calibration 相关测试，Expected: PASS。

---

### Task 7: 同步文档并完成跨 Task 验收

**Files:**
- Modify: `evals/README.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`
- Modify: `.codex/skills/langfuse-eval-engineering/references/task-design.md`
- Verify: `tests/integration/eval/`

**Interfaces:**
- Consumes: Tasks 1–6 的最终共享边界。
- Produces: 权威文档、skill workflow 和代码一致。

- [ ] **Step 1: 更新当前架构说明**

明确共享模板拥有生命周期、grader factory 拥有 Judge 编排、Task 只拥有受控世界/rubric/人工标签，
Calibration 通过版本 adapter 保持旧 fixture 可读。

- [ ] **Step 2: 运行完整 eval 故障域测试**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval
```

Expected: PASS。

- [ ] **Step 3: 运行静态与差异检查**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  evals/agent tests/integration/eval
git diff --check
```

Expected: PASS。

- [ ] **Step 4: 检查完成标准**

确认：

- 自定义 Task class 不再定义四个公共生命周期方法；
- 17 个 grader 都由 factory 绑定；
- `task.json`、Suite 和 Calibration fixture 内容未改变；
- 未调用真实 Provider、工具或 Langfuse；
- 只在本任务文件范围内形成 diff，并按仓库规则判断是否提交。
