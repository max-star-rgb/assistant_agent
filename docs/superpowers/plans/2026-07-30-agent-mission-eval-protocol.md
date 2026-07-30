# Agent Mission 评测协议实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有 Agent eval 在保持基础 Task 兼容的前提下发现 `missions/` 案例，并由通用评分入口强制执行 Environment-owned 客观终态 Rule。

**Architecture:** loader 从 `tasks/` 与 `missions/` 两个根目录建立唯一案例索引，并把来源层级作为 Git 内部事实；Calibration、CLI 和 Grading 统一从该索引解析案例目录。Mission Environment 额外提供 `objective_state_assertions(evidence)`，通用 `grade_task()` 将这些 Rule 与工具 outcome 一起写入现有 `tool_execution` dimension，不新增 Score。

**Tech Stack:** Python 3.11、Pydantic v2、pytest、现有 `evals.agent` 契约与 Langfuse 薄适配。

## Global Constraints

- 默认 Python 固定使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- pytest 必须保持 mock/local/offline，不调用真实 Chat Provider、MCP、Langfuse 或外部服务。
- Git 中的 Task、Environment、Grader 和 Calibration 是回归定义权威；Langfuse 只保存 Dataset、Experiment、Trace 和 Score。
- 保持固定的 `tool_execution`、`tool_semantics`、`grounding`、`response_quality` 四个 BOOLEAN Score，不增加 reward 或总通过状态。
- 基础 Task 不需要实现 Mission 终态方法，现有 Task ID、suite 和 Dataset item 保持兼容。
- `task.json` 不新增 `case_level`；层级只能由 loader 根据来源目录确定。
- Mission 的客观状态只能使用 Rule；缺失、空集合、Judge assertion 或异常属于评测基础设施失败。
- Task-local grader 继续只定义 `response_quality` rubric，不重复判断工具 outcome 或 Mission 终态。
- 本计划不创建具体 M10 Mission；M10 在后续计划中使用本计划产出的协议。
- 当前仓库包含用户未提交改动；执行前必须运行 `git status --short` 和相关文件的 `git diff`。修改已有
  脏文件时使用 `git add -p` 只暂存本任务 hunk；无法可靠分离时停止并请用户先整理基线，不能回滚或
  顺带提交用户改动。

---

## 文件结构

**新增**

- `tests/integration/eval/test_agent_eval_mission_protocol.py`：双根发现、重复 ID、Mission Rule、inspect 和薄 Dataset 契约的聚焦离线测试。

**修改**

- `evals/agent/loader.py`：建立双根案例索引并返回来源层级和目录。
- `evals/agent/contracts.py`：声明 Mission Environment 的 objective assertion 接口。
- `evals/agent/calibration.py`：从 loader 解析 Calibration 路径，不再硬编码 `TASKS_ROOT`。
- `evals/agent/grading.py`：验证并聚合 Mission objective assertions。
- `evals/agent/cli.py`：`--inspect` 输出案例层级、相对来源和 Mission Rule 可用性。
- `evals/README.md`：把 Mission loader、终态 Rule 和 `tool_execution` 新语义写入当前权威文档。
- `.codex/skills/langfuse-eval-engineering/SKILL.md`：同步 Mission Rule workflow，不保存案例事实。
- `.codex/skills/langfuse-eval-engineering/references/task-design.md`：补充 Mission Environment 的终态接口要求。
- `.codex/skills/langfuse-eval-engineering/references/grader-audit.md`：补充 Mission objective Rule 的审核规则。

## 执行前基线检查

- [ ] **确认当前工作树包含设计所依赖的基础 Task**

Run:

```bash
git status --short
test -f evals/agent/tasks/travel_city_poi_disambiguation/task.json
test -f evals/agent/tasks/travel_transit_route_evidence_chain/task.json
test -f evals/agent/tasks/travel_lodging_constraint_grounding/task.json
```

Expected: 三个基础 Task 存在。记录所有与本计划重叠的脏文件；不得清理、reset 或 checkout 用户改动。

- [ ] **确认提交策略**

对新文件可使用精确 `git add <new-file>`；对执行前已脏的既有文件只能使用：

```bash
git add -p <existing-dirty-file>
git diff --cached --check
git diff --cached --name-only
```

如果 patch 无法把本任务改动与用户改动分开，停止执行并报告具体文件，不创建混合提交。

### Task 1: 双根案例发现与唯一来源索引

**Files:**

- Create: `tests/integration/eval/test_agent_eval_mission_protocol.py`
- Modify: `evals/agent/loader.py`

**Interfaces:**

- Produces: `CaseLevel = Literal["task", "mission"]`
- Produces: `AgentEvalCaseSource(task_id: str, level: CaseLevel, directory: Path, relative_path: str)`
- Produces: `list_case_sources() -> list[AgentEvalCaseSource]`
- Produces: `load_case_source(task_id: str) -> AgentEvalCaseSource`
- Preserves: `list_task_ids() -> list[str]`
- Preserves: `load_task(task_id: str) -> TaskSpec`
- Preserves: `load_suite(suite_name: str) -> list[str]`

- [ ] **Step 1: 写双根发现和跨目录重复 ID 的失败测试**

在 `tests/integration/eval/test_agent_eval_mission_protocol.py` 创建临时 `tasks/`、`missions/` 根目录，
写入最小合法 `task.json`，并通过 monkeypatch 替换 loader 根目录：

```python
def _write_task(root: Path, task_id: str) -> None:
    task_dir = root / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "id": task_id,
                "description": f"{task_id} description",
                "capability": f"{task_id}_capability",
                "request": {
                    "user_id": "eval-user",
                    "session_id": "eval-session",
                    "text": "完成受控任务。",
                },
                "environment": (
                    "evals.agent.tasks.email_empty_result_honesty."
                    "environment:EmailEmptyResultEnvironment"
                ),
                "grader": (
                    "evals.agent.tasks.email_empty_result_honesty.grader:grade"
                ),
                "tags": ["offline"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_loader_discovers_tasks_and_missions_with_source_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_root = tmp_path / "tasks"
    missions_root = tmp_path / "missions"
    _write_task(tasks_root, "basic_case")
    _write_task(missions_root, "mission_case")
    monkeypatch.setattr(loader, "TASKS_ROOT", tasks_root)
    monkeypatch.setattr(loader, "MISSIONS_ROOT", missions_root)

    sources = loader.list_case_sources()

    assert [(item.task_id, item.level) for item in sources] == [
        ("basic_case", "task"),
        ("mission_case", "mission"),
    ]
    assert loader.list_task_ids() == ["basic_case", "mission_case"]
    assert loader.load_task("mission_case").id == "mission_case"


def test_loader_rejects_duplicate_ids_across_case_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_root = tmp_path / "tasks"
    missions_root = tmp_path / "missions"
    _write_task(tasks_root, "duplicate_case")
    _write_task(missions_root, "duplicate_case")
    monkeypatch.setattr(loader, "TASKS_ROOT", tasks_root)
    monkeypatch.setattr(loader, "MISSIONS_ROOT", missions_root)

    with pytest.raises(ValueError, match="duplicate.*duplicate_case"):
        loader.list_case_sources()
```

- [ ] **Step 2: 运行测试并确认因双根 API 不存在而失败**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py
```

Expected: FAIL，指出 `MISSIONS_ROOT`、`list_case_sources` 或 `AgentEvalCaseSource` 尚不存在。

- [ ] **Step 3: 在 loader 中实现唯一案例索引**

在 `evals/agent/loader.py` 增加：

```python
from dataclasses import dataclass
from typing import Literal

CaseLevel = Literal["task", "mission"]
TASKS_ROOT = Path(__file__).resolve().parent / "tasks"
MISSIONS_ROOT = Path(__file__).resolve().parent / "missions"
CASE_ROOTS: tuple[tuple[CaseLevel, str], ...] = (
    ("task", "tasks"),
    ("mission", "missions"),
)


@dataclass(frozen=True)
class AgentEvalCaseSource:
    task_id: str
    level: CaseLevel
    directory: Path
    relative_path: str


def list_case_sources() -> list[AgentEvalCaseSource]:
    roots = {
        "task": TASKS_ROOT,
        "mission": MISSIONS_ROOT,
    }
    by_id: dict[str, AgentEvalCaseSource] = {}
    duplicates: set[str] = set()
    for level, relative_root in CASE_ROOTS:
        root = roots[level]
        for path in sorted(root.glob("*/task.json")):
            if not path.is_file():
                continue
            task_id = path.parent.name
            source = AgentEvalCaseSource(
                task_id=task_id,
                level=level,
                directory=path.parent,
                relative_path=f"evals/agent/{relative_root}/{task_id}",
            )
            if task_id in by_id:
                duplicates.add(task_id)
            else:
                by_id[task_id] = source
    if duplicates:
        raise ValueError(
            "Duplicate Agent eval task_id across tasks/missions: "
            + ", ".join(sorted(duplicates))
        )
    return [by_id[task_id] for task_id in sorted(by_id)]


def load_case_source(task_id: str) -> AgentEvalCaseSource:
    by_id = {item.task_id: item for item in list_case_sources()}
    try:
        return by_id[task_id]
    except KeyError as exc:
        raise ValueError(f"Unknown Agent eval task: {task_id}.") from exc
```

把 `list_task_ids()` 改为从 `list_case_sources()` 返回 ID；把 `load_task()` 的路径改为
`load_case_source(task_id).directory / "task.json"`。保留目录名与 `TaskSpec.id` 一致性检查。

- [ ] **Step 4: 运行聚焦测试并确认通过**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py
```

Expected: PASS。

- [ ] **Step 5: 运行现有 loader/suite 邻近测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_task.py::test_release_suite_uses_non_web_batch_tasks
```

Expected: PASS，证明现有 suite 仍能解析基础 Task。

- [ ] **Step 6: 提交双根 loader**

```bash
git add evals/agent/loader.py \
  tests/integration/eval/test_agent_eval_mission_protocol.py
git commit -m "feat(eval): discover agent missions"
```

### Task 2: Calibration 与 CLI 统一使用案例来源

**Files:**

- Modify: `evals/agent/calibration.py`
- Modify: `evals/agent/cli.py`
- Modify: `tests/integration/eval/test_agent_eval_mission_protocol.py`

**Interfaces:**

- Consumes: `load_case_source(task_id: str) -> AgentEvalCaseSource`
- Produces: `calibration_path(task_id: str) -> Path`
- Produces: `_inspect_task()` 中的 `case_source.level`、`case_source.path` 和 `mission_objective_rule`

- [ ] **Step 1: 写 Mission Calibration 路径和 inspect 来源的失败测试**

在聚焦测试文件中增加：

```python
def test_calibration_path_uses_discovered_mission_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_root = tmp_path / "tasks"
    missions_root = tmp_path / "missions"
    _write_task(missions_root, "mission_case")
    calibration_file = missions_root / "mission_case" / "calibration.json"
    calibration_file.write_text(
        json.dumps({"schema_version": "agent_eval_calibration_v3", "fixtures": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "TASKS_ROOT", tasks_root)
    monkeypatch.setattr(loader, "MISSIONS_ROOT", missions_root)

    assert loader.calibration_path("mission_case") == calibration_file


def test_inspect_reports_case_level_and_relative_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = load_task("email_empty_result_honesty")
    environment = load_entrypoint(task.environment)()
    environment.objective_state_assertions = lambda evidence: {
        "synthetic_state": rule_assertion(
            True,
            f"task_id={evidence.task_id}",
            label="合成终态有效",
        )
    }
    monkeypatch.setattr(
        cli,
        "load_case_source",
        lambda _: AgentEvalCaseSource(
            task_id=task.id,
            level="mission",
            directory=Path("/tmp/mission"),
            relative_path="evals/agent/missions/mission_case",
        ),
    )
    monkeypatch.setattr(
        cli,
        "load_entrypoint",
        lambda _: lambda: environment,
    )

    payload = cli._inspect_task(task)

    assert payload["case_source"] == {
        "level": "mission",
        "path": "evals/agent/missions/mission_case",
    }
    assert payload["mission_objective_rule"]["required"] is True
```

Calibration schema 的 fixtures 最小长度为 2；该测试只检查 path helper，不加载空 fixture。

- [ ] **Step 2: 运行测试并确认硬编码路径和 inspect 输出导致失败**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py
```

Expected: FAIL，指出 `calibration_path`、`case_source` 或 `mission_objective_rule` 尚不存在。

- [ ] **Step 3: 实现路径 helper 并移除 Calibration 对 `TASKS_ROOT` 的依赖**

在 loader 中增加：

```python
def calibration_path(task_id: str) -> Path:
    return load_case_source(task_id).directory / "calibration.json"
```

在 `evals/agent/calibration.py` 中把两处：

```python
TASKS_ROOT / task.id / "calibration.json"
```

替换为：

```python
calibration_path(task.id)
```

- [ ] **Step 4: 扩展 inspect 的案例来源输出**

在 `evals/agent/cli.py` 导入 `load_case_source`，并把 `_inspect_task()` 扩展为：

```python
def _inspect_task(task: TaskSpec) -> dict[str, object]:
    source = load_case_source(task.id)
    environment = load_entrypoint(task.environment)()
    objective_method = getattr(environment, "objective_state_assertions", None)
    if source.level == "mission" and not callable(objective_method):
        raise RuntimeError(
            f"Mission {task.id!r} must define objective_state_assertions()."
        )
    return {
        "case_source": {
            "level": source.level,
            "path": source.relative_path,
        },
        "mission_objective_rule": {
            "required": source.level == "mission",
            "implemented": callable(objective_method),
        },
        "task": task.model_dump(mode="json"),
        "environment": environment.describe(),
        "environment_validation": environment.validate().model_dump(mode="json"),
        "tool_outcome_expectations": [
            expectation.model_dump(mode="json")
            for expectation in environment.tool_outcome_expectations()
        ],
    }
```

- [ ] **Step 5: 运行聚焦测试和现有离线 Calibration 集合**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py \
  tests/integration/eval/test_agent_eval_task.py::test_all_task_calibrations_match_four_dimension_labels
```

Expected: PASS。

- [ ] **Step 6: 提交路径与 inspect 改动**

```bash
git add evals/agent/loader.py evals/agent/calibration.py evals/agent/cli.py \
  tests/integration/eval/test_agent_eval_mission_protocol.py
git commit -m "feat(eval): resolve mission case assets"
```

### Task 3: 通用评分入口强制执行 Mission 客观终态 Rule

**Files:**

- Modify: `evals/agent/contracts.py`
- Modify: `evals/agent/grading.py`
- Modify: `tests/integration/eval/test_agent_eval_mission_protocol.py`

**Interfaces:**

- Produces: `MissionTaskEnvironment.objective_state_assertions(evidence) -> dict[str, AssertionResult]`
- Extends: `enforce_tool_outcome_expectations(..., objective_assertions: Mapping[str, AssertionResult] | None = None)`
- Consumes: `load_case_source(task.id).level`
- Preserves: 基础 Task 的 `tool_execution` 只有 `outcome_matches_environment`

- [ ] **Step 1: 写 objective Rule 聚合和错误分类的失败测试**

增加测试，直接验证聚合 helper：

```python
def _passed_task_judge_result() -> TaskJudgeResult:
    passed = dimension(
        {
            "judge": judge_assertion(
                JudgeVerdict(passed=True, reason="通过。"),
                criterion_id="grounding",
                label="Judge 通过",
            )
        }
    )
    return TaskJudgeResult(
        tool_semantics=passed,
        grounding=passed,
        response_quality=passed,
    )


def test_mission_objective_rules_join_tool_execution_dimension() -> None:
    evidence = RunEvidence(
        task_id="mission_case",
        run_id="run-1",
        trace_id="a" * 32,
        terminal_status="completed",
        available_tools=[],
    )
    result = enforce_tool_outcome_expectations(
        _passed_task_judge_result(),
        evidence=evidence,
        expectations=[],
        objective_assertions={
            "single_event": rule_assertion(
                False,
                "added=0",
                label="新增唯一暂定事件",
            )
        },
    )

    assert result.dimensions.tool_execution.passed is False
    assert set(result.dimensions.tool_execution.assertions) == {
        "outcome_matches_environment",
        "mission_state.single_event",
    }


@pytest.mark.parametrize(
    "objective_assertions",
    [
        {},
        {
            "judged_state": judge_assertion(
                JudgeVerdict(passed=True, reason="通过。"),
                criterion_id="grounding",
                label="错误使用 Judge 的状态检查",
            )
        },
    ],
)
def test_mission_objective_rules_must_be_nonempty_rules(
    objective_assertions: dict[str, AssertionResult],
) -> None:
    with pytest.raises(RuntimeError, match="Mission objective"):
        validate_mission_objective_assertions(objective_assertions)
```

再用 monkeypatch 让 `grade_task()` 看到 `level="mission"`，断言缺少
`objective_state_assertions` 时抛出 infrastructure `RuntimeError`；让 `level="task"` 时使用同一基础
Environment 并保持通过。

- [ ] **Step 2: 运行测试并确认 objective API 尚不存在**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py
```

Expected: FAIL，指出新的 Protocol、validator 或聚合参数尚不存在。

- [ ] **Step 3: 在 contracts 中声明 Mission Environment 接口**

在 `evals/agent/contracts.py` 增加：

```python
class MissionTaskEnvironment(TaskEnvironment, Protocol):
    def objective_state_assertions(
        self,
        evidence: RunEvidence,
    ) -> dict[str, AssertionResult]: ...
```

该 Protocol 只用于类型和文档边界；运行时仍由 grading 确定性检查 callable、返回类型和 assertion
provenance。

- [ ] **Step 4: 实现 objective assertions 校验与聚合**

在 `evals/agent/grading.py` 增加：

```python
def validate_mission_objective_assertions(
    assertions: Mapping[str, AssertionResult],
) -> dict[str, AssertionResult]:
    if not isinstance(assertions, Mapping):
        raise RuntimeError(
            "Mission objective_state_assertions() must return a mapping."
        )
    resolved = dict(assertions)
    if not resolved:
        raise RuntimeError(
            "Mission objective_state_assertions() must return at least one Rule."
        )
    invalid = [
        key
        for key, assertion in resolved.items()
        if assertion.evaluation_method != "rule"
        or assertion.criterion_id is not None
    ]
    if invalid:
        raise RuntimeError(
            "Mission objective assertions must use Rule evaluation: "
            + ", ".join(sorted(invalid))
        )
    return resolved
```

扩展 `enforce_tool_outcome_expectations()`：

```python
tool_execution_assertions = {
    "outcome_matches_environment": _tool_outcomes_match(
        evidence,
        expectations,
    )
}
for key, assertion in validate_mission_objective_assertions(
    objective_assertions
).items():
    tool_execution_assertions[f"mission_state.{key}"] = assertion
```

仅当 `objective_assertions is not None` 时调用 validator；基础 Task 继续只包含工具 outcome assertion。

- [ ] **Step 5: 让 grade_task 根据来源层级调用 Environment Rule**

在 `grade_task()` 中：

```python
source = load_case_source(task.id)
objective_assertions = None
if source.level == "mission":
    objective_method = getattr(environment, "objective_state_assertions", None)
    if not callable(objective_method):
        raise RuntimeError(
            f"Mission {task.id!r} must define objective_state_assertions()."
        )
    objective_assertions = objective_method(evidence)

return enforce_tool_outcome_expectations(
    task_result,
    evidence=evidence,
    expectations=expectations,
    objective_assertions=objective_assertions,
)
```

保留 `environment.validate().require_valid()` 在 objective Rule 之前执行；Environment validation
失败时不得生成 Agent Score。

- [ ] **Step 6: 运行聚焦测试和现有评分测试**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py \
  tests/integration/eval/test_agent_eval_task.py::test_langfuse_comments_explain_failures_without_internal_ids \
  tests/integration/eval/test_agent_eval_task.py::test_all_task_calibrations_match_four_dimension_labels
```

Expected: PASS；现有基础 Task Calibration 结果不变。

- [ ] **Step 7: 提交 Mission Rule 协议**

```bash
git add evals/agent/contracts.py evals/agent/grading.py \
  tests/integration/eval/test_agent_eval_mission_protocol.py
git commit -m "feat(eval): grade mission objective state"
```

### Task 4: 薄 Dataset 契约与权威文档同步

**Files:**

- Modify: `tests/integration/eval/test_agent_eval_mission_protocol.py`
- Modify: `evals/README.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`
- Modify: `.codex/skills/langfuse-eval-engineering/references/task-design.md`
- Modify: `.codex/skills/langfuse-eval-engineering/references/grader-audit.md`

**Interfaces:**

- Consumes: 双根 loader 与 Mission Rule 协议。
- Preserves: Langfuse Dataset item 只包含 `task_id + request + 短 metadata`。

- [ ] **Step 1: 增加 Mission 发布仍保持薄 Dataset 的测试**

使用临时 Mission `TaskSpec` 和现有 `_FakeLangfuseClient` 等价 fake，断言：

```python
item = client.items[0]
assert item["input"] == {
    "task_id": mission.id,
    "request": mission.request.model_dump(mode="json"),
}
assert item["metadata"] == {
    "task_id": mission.id,
    "capability": mission.capability,
    "tags": mission.tags,
}
assert "case_level" not in item["metadata"]
assert "environment" not in item["metadata"]
assert "objective_state" not in item["metadata"]
assert "grader" not in item["metadata"]
```

- [ ] **Step 2: 运行测试并确认薄 backend 无需生产代码扩展**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval/test_agent_eval_mission_protocol.py
```

Expected: PASS；如果失败，只修复通用 `publish_tasks()` 的兼容性，不增加 Mission 私有 metadata。

- [ ] **Step 3: 更新 `evals/README.md` 当前协议**

写明：

- `tasks/` 与 `missions/` 只是组织层级，共用运行协议；
- loader 跨目录拒绝重复 ID；
- Mission Environment 必须实现非空 Rule-only `objective_state_assertions()`；
- `tool_execution` 对基础 Task 仍是工具 outcome，对 Mission 是工具 outcome 加 objective state；
- inspect、calibrate、publish、run 和 Scores v3 审计顺序不变；
- Mission Rule、Environment 或 Evidence 故障退出 2。

- [ ] **Step 4: 同步 eval skill 和两份 reference**

只同步 workflow 约束：

- 选择 Mission 时先确认终态能由结构化 state Evidence 证明；
- Task grader 不拥有 objective Rule；
- `--inspect` 必须显示案例层级和 Mission Rule 是否实现；
- Langfuse Dataset 不复制 case level、state oracle 或 rubric。

不要把 M10 地点、酒店或路线 fixture 写进通用 skill。

- [ ] **Step 5: 运行整个 eval pytest 故障域**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval
```

Expected: PASS。该变更跨 loader、Calibration、CLI 和 Grading，属于同一 eval 故障域，运行整个目录是
最小充分验证；不默认运行全量 pytest。

- [ ] **Step 6: 运行离线 inspect 冒烟**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py --inspect --task email_empty_result_honesty
```

Expected: exit 0；stdout JSON 中 `case_source.level="task"`、
`mission_objective_rule.required=false`，且不读取 `.env`、不联网。

- [ ] **Step 7: 检查文档与 diff**

Run:

```bash
rg -n "objective_state_assertions|missions/|tool_execution" \
  evals/README.md \
  .codex/skills/langfuse-eval-engineering/SKILL.md \
  .codex/skills/langfuse-eval-engineering/references
git diff --check
```

Expected: 所有协议位置均可检索，无 whitespace error。

- [ ] **Step 8: 提交协议文档和最终测试**

```bash
git add evals/README.md \
  .codex/skills/langfuse-eval-engineering/SKILL.md \
  .codex/skills/langfuse-eval-engineering/references/task-design.md \
  .codex/skills/langfuse-eval-engineering/references/grader-audit.md \
  tests/integration/eval/test_agent_eval_mission_protocol.py
git commit -m "docs(eval): define agent mission protocol"
```

## 计划完成时的汇报

```text
Tests: added test_agent_eval_mission_protocol.py because loader discovery and
Mission objective grading are new observable eval contracts.
```

同时报告：

- 实际运行的 pytest 命令；
- `--inspect` 是否保持离线；
- 未运行全量 pytest，因为变更可由 `tests/integration/eval` 故障域充分证明；
- 未调用真实 Provider、Langfuse、地图、住宿或日历服务。
