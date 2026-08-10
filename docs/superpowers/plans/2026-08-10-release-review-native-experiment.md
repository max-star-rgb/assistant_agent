# 上线前 Release Review 原生 Experiment 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一个十分钟内完成的 Langfuse 原生 Experiment 取代旧 Agent eval，使重大 Agent 变更通过 Decision fixture 与真实 Staging 场景获得可比较的上线前人工评审证据。

**Architecture:** Git 中的声明式 YAML 同步到单一 `assistant-agent-release-review` Dataset；Langfuse 原生 Experiment task 按 Item phase 使用相同 production Registry 和 assistant loop，但分别选择 fixture execution backend 或真实 Registry backend。确定性契约写入 `task_conformance`，语义质量复用现有 `grounding/response_quality` Evaluator，基础设施故障单独记录，最后由人作发布决定。

**Tech Stack:** Python 3.12、Pydantic 2、PyYAML 6、AgentGraphRuntime、ToolExecutor/ToolRegistry、Langfuse Python SDK 4.x、FastAPI、pytest。

## Global Constraints

- 只在模型、Prompt、工具目录或 Agent Runtime 重大变更时显式运行，不进入普通 CI。
- 一次 Release Review 全局硬超时 570 秒：Decision 180 秒、Staging 300 秒、汇总 60 秒。
- pytest 固定 mock/offline，不访问真实 Provider、Langfuse、MCP 或 Staging。
- 真实运行要求 real mode、独立预发布账号/数据库/MCP namespace 和 operator 显式开关。
- Decision backend 只替换执行动作，不改变 Registry、ToolSpec、catalog、Validator 或 assistant loop。
- Staging 写操作使用 `release_id + scenario_id` 隔离并报告 cleanup，禁止使用正式用户资源。
- 复用 `assistant_agent.quality.task_conformance/grounding/response_quality`；基础设施状态和人工决定不是质量 Score。
- 日常 Trace、Live Observation Rule 和 runtime audit 行为保持不变。
- Core invariant `TOOL-001` 扩展为：默认 backend 仍执行 Registry Tool；受信 Scenario backend 只能在 ToolExecutor 内替换无副作用 invocation，validation、catalog、state lifecycle 和 trace 不得绕过。
- 临时测试放入 `tests/tdd/release-review-native-experiment/`，不自动晋升 core。

---

### Task 1: 声明式场景契约与 Loader

**Files:**
- Modify: `pyproject.toml`
- Create: `evals/release_review/__init__.py`
- Create: `evals/release_review/contracts.py`
- Create: `evals/release_review/loader.py`
- Test: `tests/tdd/release-review-native-experiment/test_scenario_loader.py`

**Interfaces:**
- Produces: `ReleaseScenario`、`ToolContract`、`ToolArgumentAssertion`、`StateAssertion`、`ToolFixture`、`StagingContract`。
- Produces: `load_scenario(path: Path) -> ReleaseScenario`、`load_scenarios(root: Path) -> tuple[ReleaseScenario, ...]`、`scenario_hash(scenario: ReleaseScenario) -> str`。
- Invariant: `yaml.safe_load`、Pydantic `extra="forbid"`、ID 唯一；Decision 必须有 fixture，Staging 必须有 resource profile 与 cleanup；`repetitions` 只能为 1 或 2，Critical Decision 固定为 2。

- [ ] **Step 1: 声明依赖并写 RED 测试**

在 `eval` extra 增加 `PyYAML>=6.0,<7`。测试用 `tmp_path` 写 Decision/Staging YAML，覆盖未知字段、重复 ID、required/forbidden 冲突、Decision 缺 fixture、Staging 缺 cleanup 和稳定 hash。

```python
def test_decision_requires_fixture_for_required_tools(tmp_path):
    path = write_yaml(tmp_path, DECISION_WITHOUT_FIXTURE)
    with pytest.raises(ValueError, match="missing fixtures"):
        load_scenario(path)
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_scenario_loader.py
```

- [ ] **Step 3: 实现契约和 Loader**

`ToolArgumentAssertion` 只允许 `equals/contains/gte/exists/length` 且恰有一个生效；sequence 使用 `before + before_final_response`。`scenario_hash()` 对 `model_dump(mode="json", exclude_none=True)` canonical JSON 做 SHA-256。所有错误携带场景文件名。

- [ ] **Step 4: 运行测试并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_scenario_loader.py
git diff --check -- pyproject.toml evals/release_review tests/tdd/release-review-native-experiment
git add pyproject.toml evals/release_review tests/tdd/release-review-native-experiment/test_scenario_loader.py
git commit -m "feat(eval): add release review scenario contracts"
```

### Task 2: ToolExecutionBackend 与 Runtime 注入

**Files:**
- Create: `src/assistant_agent/runtime/tool_execution_backend.py`
- Modify: `src/assistant_agent/runtime/tool_executor.py`
- Modify: `src/assistant_agent/runtime/runtime.py`
- Modify: `tests/core/INVARIANTS.md`
- Modify: `tests/core/contract/test_tool_contract.py`
- Test: `tests/tdd/release-review-native-experiment/test_tool_execution_backend.py`

**Interfaces:**
- Produces: `ToolExecutionBackend.run(registry, tool_name, tool_input, context) -> ToolResult` Protocol。
- Produces: `RegistryExecutionBackend`，默认调用 `registry.run(...)`。
- Extends: `ToolExecutor(..., execution_backend: ToolExecutionBackend | None = None)`。
- Extends: `AgentGraphRuntime(..., tool_execution_backend: ToolExecutionBackend | None = None)`。

- [ ] **Step 1: 写默认治理链与自定义 backend RED 测试**

Core `TOOL-001` 断言默认 Probe Tool 仍通过 Registry 实际执行。临时 TDD 传入 recording backend，断言其接收 seal 后 Registry、规范 tool name、绑定输入和 ToolContext，且 `tool.started/tool.finished` 与 AgentState 正常提交。

```python
class RecordingBackend:
    def run(self, registry, tool_name, tool_input, context):
        self.calls.append((registry, tool_name, tool_input, context))
        return ToolResult(tool_name=tool_name, success=True, data={"sentinel": 1})
```

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_tool_execution_backend.py tests/core/contract/test_tool_contract.py
```

- [ ] **Step 3: 实现 backend 与 Runtime 注入**

`ToolExecutor._run_with_retry()` 的 invocation 改为 `self.execution_backend.run(self.registry, ...)`。绑定、重试、取消、recovery、state commit 与 trace 均留在 ToolExecutor。Runtime 只能由受信 composition root 显式注入 backend，不读取请求文本或 metadata 决定 backend。

- [ ] **Step 4: 更新 TOOL-001、验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/core/contract/test_tool_contract.py tests/core/integration/test_runtime_lifecycle.py tests/tdd/release-review-native-experiment/test_tool_execution_backend.py
git add src/assistant_agent/runtime tests/core/INVARIANTS.md tests/core/contract/test_tool_contract.py tests/tdd/release-review-native-experiment/test_tool_execution_backend.py
git commit -m "feat(runtime): add governed tool execution backend"
```

### Task 3: Decision backend、Evidence 与确定性断言

**Files:**
- Create: `evals/release_review/decision_backend.py`
- Create: `evals/release_review/evidence.py`
- Create: `evals/release_review/assertions.py`
- Test: `tests/tdd/release-review-native-experiment/test_decision_backend.py`
- Test: `tests/tdd/release-review-native-experiment/test_release_assertions.py`

**Interfaces:**
- Produces: `ScenarioExecutionBackend(scenario: ReleaseScenario)` implements `ToolExecutionBackend`。
- Produces: `ReleaseRunEvidence.from_state(state, events) -> ReleaseRunEvidence`。
- Produces: `evaluate_task_conformance(scenario, evidence) -> ConformanceResult`，包含 `passed` 与稳定 `key/label/reason` assertions。

- [ ] **Step 1: 写 backend 与 assertion RED 测试**

覆盖 fixture 成功/失败、同 Tool 多次返回、未知 fixture 的 `release_fixture_missing`、结果深拷贝、required/forbidden/allowed、五种参数操作符、调用偏序和终态断言。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_decision_backend.py tests/tdd/release-review-native-experiment/test_release_assertions.py
```

- [ ] **Step 3: 实现 backend、Evidence 与 conformance**

Backend 只接受 fixture 中的 Tool；调用记录保存 `tool_name/input/call_index/status`。Evidence 从 AgentState 和 canonical Tool events 投影，不保存 Provider raw response。Assertion 顺序固定为 required、forbidden、allowed、arguments、sequence、state；infrastructure error 必须在调用前被排除。

- [ ] **Step 4: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_decision_backend.py tests/tdd/release-review-native-experiment/test_release_assertions.py
git add evals/release_review tests/tdd/release-review-native-experiment
git commit -m "feat(eval): evaluate release tool decisions"
```

### Task 4: Staging 资源、catalog 与 cleanup

**Files:**
- Create: `evals/release_review/catalog.py`
- Create: `evals/release_review/staging.py`
- Test: `tests/tdd/release-review-native-experiment/test_release_catalog.py`
- Test: `tests/tdd/release-review-native-experiment/test_staging_resources.py`

**Interfaces:**
- Produces: `build_catalog_snapshot(registry) -> ReleaseCatalogSnapshot`。
- Produces: `StagingResourceManager.prepare(release_id, scenario) -> StagingLease`。
- Produces: `StagingLease.runtime_metadata`、`StagingLease.cleanup() -> CleanupResult`。
- 真实执行复用 `RegistryExecutionBackend`，不建立第二实现。

- [ ] **Step 1: 写 catalog/cleanup RED 测试**

断言 generation 对排序后的 ToolSpec 稳定、required Tool 缺失预检失败、namespace 安全且确定、cleanup 幂等、部分失败保留资源 ref 和 infrastructure status。

- [ ] **Step 2: 实现固定 profile 资源管理**

首版只允许 `deep_research_workflow`、`amap_readonly`、`test_calendar`。adapter map 来自服务 composition root，不从 YAML import Python。只读 cleanup 为 skipped；写 profile 调固定 adapter，结果完整记录。

- [ ] **Step 3: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_release_catalog.py tests/tdd/release-review-native-experiment/test_staging_resources.py
git add evals/release_review tests/tdd/release-review-native-experiment
git commit -m "feat(eval): govern release staging resources"
```

### Task 5: Dataset 同步、原生 Experiment、报告与 CLI

**Files:**
- Create: `evals/release_review/langfuse_backend.py`
- Create: `evals/release_review/experiment.py`
- Create: `evals/release_review/sync_dataset.py`
- Create: `evals/release_review/report.py`
- Create: `evals/release_review/service.py`
- Create: `evals/release_review/cli.py`
- Create: `scripts/run_release_review.py`
- Test: `tests/tdd/release-review-native-experiment/test_dataset_sync.py`
- Test: `tests/tdd/release-review-native-experiment/test_native_release_experiment.py`
- Test: `tests/tdd/release-review-native-experiment/test_release_service.py`

**Interfaces:**
- Constant: `RELEASE_REVIEW_DATASET = "assistant-agent-release-review"`。
- Produces: `sync_release_dataset(client, scenarios, git_commit) -> DatasetSyncResult`。
- Produces: `run_release_experiment(client, scenarios, settings, progress=None) -> ReleaseExperimentResult`。
- Produces: `ReleaseReviewService.run(request) -> ReleaseReviewReport` 与 `record_release_decision(...) -> ReleaseDecisionRecord`。
- CLI actions: `--inspect`、`--sync`、`--run`、`--record-decision`。

- [ ] **Step 1: 写 Dataset 与 Experiment RED 测试**

Fake client 验证单 Dataset、稳定 Item ID、input/expected_output/metadata 边界、归档仅作用 Git-owned Item。Fake Dataset 捕获 `run_experiment(task, evaluators, max_concurrency=4)`；Decision 注入 Scenario backend，Staging 使用默认 backend；本地 evaluator 只生成 canonical `task_conformance`。

- [ ] **Step 2: 写 service/report RED 测试**

覆盖 570 秒 deadline、Critical/High/flaky/infrastructure 分类、approved baseline 可比性、三种人工决定、安全决策记录、单 Item failure 不伪装成 false Score、全局超时仍 cleanup。

- [ ] **Step 3: 实现 native Experiment**

同步时把每个 scenario 按 `repetitions` 展开为独立原生 Dataset Item，ID 为 `assistant-agent-release-review__<scenario_id>__r<index>`；普通场景只有 `r1`。Task 重新加载 Git 场景并核对 hash，Decision 创建 Scenario backend，Staging 获取 lease 并 finally cleanup。Experiment metadata 写 `evaluation_mode=release_review`、phase/release/model/prompt/catalog/evaluator version。`grounding/response_quality` 由 Langfuse UI 选择现有原生 Evaluator；落库审计要求每个 Item 的三个 task-level Score 各一份，报告再按 scenario ID 聚合 flaky。

- [ ] **Step 4: 实现 service、报告和 CLI**

固定流程为 load/validate -> sync/preflight -> Experiment -> flush/Score audit -> baseline compare -> JSON/Markdown report。artifact 写 `.data/evals/release_review/<release_id>/`。`--run` 要求 real mode、`--allow-real-provider` 和 staging readiness；`--inspect` 不读 `.env`、不联网、不建真实 Registry。

- [ ] **Step 5: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment/test_dataset_sync.py tests/tdd/release-review-native-experiment/test_native_release_experiment.py tests/tdd/release-review-native-experiment/test_release_service.py
git add evals/release_review scripts/run_release_review.py tests/tdd/release-review-native-experiment
git commit -m "feat(eval): run native release review experiments"
```

### Task 6: Remote webhook 与首批场景

**Files:**
- Create: `src/assistant_agent/evaluation/release_review.py`
- Modify: `src/assistant_agent/api/routes_eval_experiments.py`
- Modify: `deploy/langfuse_eval_webhook/webhook_proxy.py`
- Modify: matching compose/env templates found by `rg 'REMOTE_EXPERIMENT' deploy`
- Create: `evals/release_review/scenarios/*.yaml`（8 Decision、3 Staging）
- Test: `tests/tdd/release-review-native-experiment/test_release_review_webhook.py`
- Test: `tests/tdd/release-review-native-experiment/test_initial_scenarios.py`

**Interfaces:**
- Endpoint: `POST /internal/evals/langfuse/release-review`。
- Payload: `releaseId`、`model`、`promptVersion`、可选 scenario 白名单、`runName`；`extra="forbid"`。
- Initial IDs: `deep_research_admission`、`simple_request_no_workflow`、`deep_research_constraints`、`tool_failure_no_repeat`、`correct_tool_among_candidates`、`write_requires_precondition`、`wait_for_tool_result`、`no_unobserved_result_claim`、`staging_deep_research_workflow`、`staging_amap_read_chain`、`staging_calendar_write_read_cleanup`。

- [ ] **Step 1: 写 webhook 与场景 RED 测试**

覆盖签名过期/错误、固定 Dataset、额外字段拒绝、安全 identifier、重复 trigger 幂等和固定 argv。场景测试断言 8/3 场景数量、ID 唯一、Critical repetitions=2、写场景 cleanup、请求不泄露测试机关，并验证同步后的 Item 数等于 repetitions 总和。

- [ ] **Step 2: 实现 launcher/route/proxy**

复用现有 `RemoteProgressTracker` 和 reaper/idempotency，但固定启动 `scripts/run_release_review.py --run`。env 改为 `ASSISTANT_AGENT_LANGFUSE_RELEASE_REVIEW_*`，不静默兼容旧 env。Proxy 只转发新固定 path并保持 HMAC。

- [ ] **Step 3: 创建 11 个场景**

只从旧案例提取用户请求与仍有价值的结构化契约，不翻译 Environment/grader/calibration。Decision fixture 保持最小结构化 ToolResult；Staging 仅用三个固定 profile。

- [ ] **Step 4: 验证并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment
git add src/assistant_agent/evaluation/release_review.py src/assistant_agent/api deploy evals/release_review/scenarios tests/tdd/release-review-native-experiment
git commit -m "feat(eval): expose native release review workflow"
```

### Task 7: 删除旧链、同步权威文档并最终验证

**Files:**
- Delete: `evals/agent/`
- Delete: `scripts/run_agent_evals.py`
- Delete: `src/assistant_agent/evaluation/remote_experiment.py`
- Delete: obsolete Agent eval/remote experiment TDD directories
- Modify: `src/assistant_agent/runtime/runtime.py`（删除 `registry_transform`）
- Modify: `evals/README.md`
- Modify: `docs/tool-calling-architecture.md`
- Modify: `docs/observability-harness.md`（只更新相邻边界）
- Modify: `docs/authority.toml`
- Modify: `scripts/README.md`
- Modify: `.codex/skills/langfuse-eval-engineering/SKILL.md`
- Modify: `AGENTS.md` 和导航引用（仅活动入口名）

**Interfaces:**
- `evals/README.md` 成为 Release Review、Dataset、Experiment、Score、webhook 和真实 Staging 的唯一权威。
- `docs/observability-harness.md` 继续只拥有日常 Trace/Live Rule/runtime audit。

- [ ] **Step 1: 解析引用并删除旧链**

使用 `rg 'evals\.agent|run_agent_evals|registry_transform|remote-experiment'` 列出活动引用，再用 `apply_patch` 删除旧文件。Runtime 恢复单一 production Registry 装配；Release Review 只通过 `tool_execution_backend` 注入。历史 spec/plan 可保留旧词，活动源码/文档不得残留。

- [ ] **Step 2: 更新权威文档、skill 和命令**

Eval 文档写单 Dataset、两 phase、canonical Score 复用、基础设施归因、十分钟预算、人工决定和 staging 副作用。Tool 文档写 execution backend 并删 overlay。Observability 只说明两条运行线独立但复用 Score 契约。Authority 改为 `run_release_review.py --help/--inspect`。

- [ ] **Step 3: 运行静态、core 与新 TDD**

```bash
git diff --check
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q
MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/tdd/release-review-native-experiment
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py --inspect
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/check_documentation_authority.py
```

- [ ] **Step 4: 提交切换**

```bash
git add -A evals src/assistant_agent/evaluation src/assistant_agent/runtime/runtime.py scripts tests/tdd deploy docs AGENTS.md .codex/skills/langfuse-eval-engineering
git commit -m "refactor(eval): replace agent eval with release review"
```

- [ ] **Step 5: 条件满足时做真实预发布验收**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_release_review.py --run --release-id rc-native-release-review-01 --model qwen-plus --prompt-version release-review-v1 --allow-real-provider
```

核对总时长、11 个场景按 repetitions 展开的全部 Item、每个 Item 的三项 task-level Score、真实 Tool observation、cleanup、Run Comparison 和报告。缺少 staging 或 operator 开关时必须报告未执行，禁止 mock fallback。

## Verification Summary

```text
Core invariant: TOOL-001 changed because ToolExecutor gains a trusted execution backend while retaining validation, Registry contract lookup, lifecycle and default Registry invocation.
Tests: update existing TOOL-001 core coverage and add temporary tests/tdd/release-review-native-experiment for RED/GREEN; user may delete that directory manually after the feature stabilizes.
```

真实 Provider 验收不属于 pytest；若本机缺少 staging 或 operator 开关，不得声称完成生产验收。
