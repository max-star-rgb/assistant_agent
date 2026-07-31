# Agent Eval Task 收敛重构设计

## 目标

降低 `evals/agent/tasks/**` 对共享评测协议变化的敏感度。以后修改 Environment 生命周期、默认工具
目录、工具 outcome 生成、Judge 调用方式或 Calibration 读取机制时，应优先只修改共享框架；Task
目录只在用户挑战、受控世界、Task 专属回答标准或人工校准标签发生变化时修改。

本次重构保持现有 Task 请求、活动 `AgentGraphRuntime`、工具行为、Evidence、四项独立 Score、
Langfuse Dataset/Experiment 契约和真实 Provider 安全开关不变。

## 当前问题

当前 17 个 Task 具有以下重复：

- 9 个自定义 Environment 重复实现 `describe()`、`validate()`、
  `tool_outcome_expectations()`、`execute()` 和 `_build_registry()`；
- 8 个批量 Task 已使用 `BatchCaseEnvironment`，但该类又独立实现同一套生命周期；
- 17 个 `grader.py` 都重复定义相同的 `grade()` 调用壳，只有
  `RESPONSE_QUALITY_RUBRIC` 不同；
- Calibration 读取直接绑定单一 Pydantic schema，公共 schema 变化容易迫使所有
  `calibration.json` 同步迁移。

根因是共享评测协议以命令式模板散落在 Task-local 文件中，而不是由共享框架拥有。

## 设计边界

### 共享框架拥有

- Environment 的初始化、描述、通用验证、完整工具目录检查；
- 默认完整工具目录和结构化 `tool_visibility` override；
- outcome expectation 的完整覆盖、required success/failure 和 Evidence 可见子集处理；
- Runtime 隔离、执行、Evidence 投影、初始/最终状态及 diff；
- `tool_semantics`、`grounding`、`response_quality` 三项 Judge 的统一调用和结果组装；
- Calibration schema 识别、版本分派和规范化。

### Task 本地拥有

- `task.json` 中的用户请求、capability、入口和 tags；
- 冻结依赖、临时文件、模拟 adapter/MCP runner 和故障注入；
- required success/failure 工具及 Task 专属 validation assertions；
- 初始状态、最终状态读取和必要的 runtime overrides；
- `RESPONSE_QUALITY_RUBRIC`；
- 人工标注的 Calibration Evidence、Judge verdict 和四项期望 Score。

人工 oracle 和校准标签不能为了减少文件数量而移入共享框架。

## Environment 模板

新增共享 `ControlledTaskEnvironment`，集中实现 `TaskEnvironment` 协议：

```python
class ControlledTaskEnvironment:
    def describe(self) -> dict[str, Any]: ...
    def validate(self) -> EnvironmentValidation: ...
    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]: ...
    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest | dict[str, Any],
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution: ...
```

Task 子类只覆盖窄 hooks：

```python
@dataclass(frozen=True)
class EnvironmentToolVisibility:
    profile: str
    allowed_tools: tuple[str, ...]


class ExampleEnvironment(ControlledTaskEnvironment):
    dependency_label = "controlled:example"
    writes = False

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

除 `build_registry()` 和 `task_validation_checks()` 外，其余 hooks 提供行为中性的默认实现；
`visibility_override()` 默认返回 `None`。共享类惰性缓存已 seal 的 registry，避免一次
inspect/execute 重复装配不同实例。

### 通用验证

`validate()` 固定检查：

1. Registry 已 seal；
2. 默认 Environment 的工具集合符合共享完整目录契约，或显式 visibility profile 已通过验证；
3. required success/failure 都属于 registry；
4. outcome expectation 无重复且完整覆盖最终可见工具；
5. Task-local validation assertions 全部采用 Rule。

Task 子类只返回受控依赖、冻结数据、隔离状态等专属 assertions。验证失败仍属于评测基础设施错误，
不生成 Agent Score。

### 工具可见性

默认 Environment 使用完整受控 Agent eval registry。特殊场景可以由 Environment 或受信入口提供
结构化 `metadata.tool_visibility.profile + allowed_tools`。共享模板验证 allowlist 是 registry
子集，并基于最终可见集合生成 outcome expectation；它不能根据用户自然语言、capability、grader
或校准答案预选工具，也不能扩大真实工具权限。

### 执行和状态

共享 `execute()` 依次执行：

1. `validate().require_valid()`；
2. 规范化 `UserRequest` 并应用 Environment-owned visibility profile；
3. 获取初始状态和 runtime overrides；
4. 通过现有 `execute_isolated_runtime()` 运行活动 `AgentGraphRuntime`；
5. 调用可选 before-run/final-state hooks；
6. 返回现有 `TaskExecution` 和 `RunEvidence`。

Task 不得覆盖 `execute()` 来替 Agent 选择工具、构造参数、重试或生成最终回答。

## Grader factory

共享评分模块增加稳定 factory：

```python
def grader_for_response_quality(
    rubric: str,
) -> Callable[[RunEvidence, LLMJudge], TaskJudgeResult]: ...
```

Task-local `grader.py` 只保留：

```python
RESPONSE_QUALITY_RUBRIC = """...""".strip()
grade = grader_for_response_quality(RESPONSE_QUALITY_RUBRIC)
```

factory 内部调用现有三项 Judge 并组装 `TaskJudgeResult`。以后 criterion、Judge transport 或结果组装
变化只修改共享模块，不再修改全部 Task grader。`task.json` 的 grader entrypoint 保持 `:grade`，
避免 Dataset 和 loader 契约变化。

## Calibration 兼容层

新增唯一的 `load_calibration_set(task_id)`：

1. 先读取原始 `schema_version`；
2. 按版本选择严格 Pydantic model；
3. 规范化为当前内部 `CalibrationSet`；
4. 未知版本明确报告基础设施错误。

本次不修改现有人工标签，也不降低 `expected_dimensions` 和三个 `judge_verdicts` 的显式性。以后新增
schema 时增加版本 adapter，旧 Task 可继续读取；只有语义确实需要重新标注时才批量迁移 fixture。

## 迁移范围

1. 新增共享 Environment 模板及其契约测试；
2. 让 `BatchCaseEnvironment` 继承共享模板；
3. 按天气、旅行、文件、邮件四组迁移 9 个自定义 Environment，只保留 hooks；
4. 将 17 个 grader 改为稳定 factory 绑定；
5. 将 Calibration 的两个读取入口统一到版本化 loader；
6. 同步 `evals/README.md`、项目 skill 和邻近测试。

`task.json`、用户请求、Task ID、Suite、Calibration fixture 内容和 Langfuse Dataset item 不迁移。

## 错误处理

- registry、visibility profile、required outcome 或 Task-local Rule 不合法：
  `EnvironmentValidation` 失败；
- Task hook 返回错误类型、重复 assertion 或 Judge assertion：基础设施错误；
- Calibration schema 未知或 adapter 无法规范化：基础设施错误；
- Judge、Trace、Langfuse 和 Score 持久化错误继续沿用现有退出码 2；
- Agent 行为结果仍只反映在四项 BOOLEAN Score，不因框架错误被改写。

## 测试策略

按照风险驱动测试：

- 共享模板单独验证默认完整目录、结构化 override、required success/failure、通用验证和状态 hooks；
- 遍历全部 Task，验证 Environment 入口、默认目录、outcome 完整性和 `validate()`；
- 遍历全部 grader，验证 factory 仍产生三个独立 Judge criterion；
- 使用现有人工 fixture 验证 Calibration loader 和四项结果不变；
- 运行 `tests/integration/eval` 作为跨 Task wiring 验收；
- pytest 全程使用 mock/local/offline，不调用真实 Provider、MCP、Langfuse 或外部服务。

## 完成标准

- 所有现有 Task 的 inspect、校准契约和四项评分行为保持不变；
- Task Environment 不再重复实现五个公共生命周期方法；
- Task grader 不再手写 `grade()` 调用壳；
- 修改共享 Environment 或 Judge 编排只需修改共享模块和共享测试；
- Calibration 新版本可通过 adapter 引入，不要求旧 fixture 同步改写；
- Task 目录仍是用户挑战、受控世界、rubric 和人工校准事实的权威。
