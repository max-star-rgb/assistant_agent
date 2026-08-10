# 上线前 Release Review 原生 Experiment 设计

## 1. 目标

为模型、Prompt、工具目录或 Agent Runtime 的重大变更提供一次十分钟内完成的上线前人工评审。
系统首先识别 Agent 是否漏调、误调工具或构造错误参数，其次验证少量关键能力能否在独立预发布资源中
真实完成。系统提供证据和风险结论，但不自动决定是否发布。

本系统只负责上线前评审。日常 Trace、Live Observation Evaluator、runtime audit 和线上长尾问题发现
属于另一条既有链路，本次不得改变其运行、采样、Score 或审计行为。

## 2. 已确认约束

- 只在模型、Prompt、工具目录或 Agent Runtime 发生重大变更时运行，不进入每次提交的普通 CI。
- 使用独立的预发布账号、MCP、数据库和 namespace，禁止访问正式用户资源。
- 一次完整评审必须在十分钟内结束。
- 评审结果由人决定为 `approved`、`approved_with_risk` 或 `rejected`，不做自动发布门禁。
- 首要风险是 Agent 选错或漏用工具；真实工具链和回答质量是次级证据。
- 充分使用 Langfuse 原生 Dataset、Experiment、Evaluator、Trace、Score 和 Run Comparison。
- Git 保存场景事实；Langfuse 保存协作数据、运行证据与历史比较。

## 3. 总体架构

```text
Git YAML 场景
  -> Dataset sync
  -> assistant-agent-release-review Dataset
  -> Langfuse 原生 Experiment Run
       -> decision items: 真实模型/Prompt/ToolSpec + fixture execution backend
       -> staging items: 真实模型/Prompt/ToolSpec + 预发布真实 execution backend
  -> 确定性 task_conformance + 原生 grounding/response_quality
  -> Run Comparison + 风险报告
  -> 人工发布决定
```

所有 Item 位于一个 Dataset，通过 `metadata.phase=decision|staging` 区分阶段。一次 Release Review 只
产生一个 Experiment Run，便于在 Langfuse 中直接与最近一次人工批准 Run 比较。

Langfuse 是运行、评分、比较和展示平台；它不拥有工具权限、凭据、生产配置或自然语言之外的 Agent
决策逻辑。仓库不重复实现 Langfuse 已提供的 Dataset Run、Experiment 记录和 Run Comparison。

## 4. 场景契约

场景采用声明式 YAML，不再使用“一案例一个 Python Environment/Grader”。每个场景包含：

- `id`、`phase`、`capability`；
- 真实用户 `request`；
- `required`、`allowed`、`forbidden` 工具集合；
- 参数约束与必要的偏序调用约束；
- Decision 阶段使用的确定性 `fixtures`；
- Staging 阶段使用的资源 profile、终态断言和清理契约；
- 各类失败的风险等级。

参数和终态断言只允许有限的结构化操作符，例如 `equals`、`contains`、`gte`、`exists` 和列表长度，
禁止场景加载任意 Python grader。工具契约、fixture、终态 oracle 和 expected output 不得进入 Agent
input 或 Provider payload。

Dataset Item 映射固定为：

```text
input: scenario_id + request
expected_output: tool contract + argument/state assertions
metadata: phase + capability + scenario_hash + git_commit + model + prompt_version
          + catalog_generation + evaluator_version
```

Git YAML 是事实源。同步入口先做 schema 校验，再按稳定 Item ID upsert；本地已删除场景在 Dataset 中
归档，不允许同 ID 不同定义静默覆盖。

## 5. 两种执行阶段

### 5.1 Decision Probe

Decision Item 使用真实 Chat Provider、Prompt、Context 组装、assistant loop、预发布 Registry 和模型可见
ToolSpec。工具调用仍经过 `ActionValidator`，但 `ToolExecutor` 把执行交给
`ScenarioExecutionBackend`，由场景 fixture 返回确定性 `ToolResult`，不发起外部调用或写操作。

这一边界要求 Runtime 新增通用 `ToolExecutionBackend`：

- `RegistryExecutionBackend`：默认生产与 Staging 路径，通过 Registry 执行真实 Tool；
- `ScenarioExecutionBackend`：Decision 路径按场景返回 fixture，并拒绝未声明调用。

后端只改变 Tool 的执行结果，不改变 Registry、Tool name、ToolSpec、可见目录、Validator 或 assistant
loop。由此不再需要模拟 Registry、同名 Tool replacement 或 eval 专属 `registry_transform`。

Decision 计划保留 8 至 12 个高价值场景，并发度默认 4。Critical 场景在同步时展开为两个独立 Dataset
Item，以便每次尝试都拥有原生 Trace 和 Score；普通场景展开为一个 Item。报告按 scenario ID 聚合，
一次关键失败不能被平均分掩盖。

### 5.2 Staging Smoke

Staging Item 使用 `RegistryExecutionBackend` 和独立预发布资源，真实运行 Provider、Validator、Executor、
MCP、数据库与写入链。只保留 3 至 4 个关键场景，并发度默认 2：

1. Deep Research Workflow 创建并从预发布 store 读回；
2. 一个真实 MCP 读取链；
3. 一个测试日历写入、读取和清理链；
4. 只有在十分钟预算允许时增加一个依赖失败恢复场景。

每个写场景必须使用 `release_id + scenario_id` 派生的 namespace，记录资源引用并执行 cleanup。清理失败
属于显式基础设施风险，不得隐藏或伪装成 Agent Score。

## 6. Langfuse 原生运行与入口

保留签名 Remote Experiment 入口并改造成 Release Review。Langfuse Dataset UI 的原生
`Run Experiment` 调用受签名 webhook，服务端以 Langfuse SDK 执行 Dataset Experiment task 和
Evaluator。请求只允许：

- `releaseId`；
- `model`；
- `promptVersion`；
- 可选精确场景白名单；
- `runName`。

请求不得传 shell、环境变量、env file、凭据、工具权限、任意路径或副作用开关。预发布权限完全来自
服务启动配置和固定 release profile。

CLI 同时提供等价入口：

```bash
scripts/run_release_review.py \
  --release-id rc-YYYY-MM-DD-NN \
  --model <model> \
  --prompt-version <version>
```

CLI 与 webhook 必须调用同一个 application service，不得各自实现 Experiment 逻辑。

## 7. 评分复用与故障归因

上线前与日常运行是两条独立流程，但测量相同质量时复用现有 canonical Score 与版本化 Evaluator：

- `assistant_agent.quality.task_conformance`：上线前确定性工具与终态契约；
- `assistant_agent.quality.grounding`：复用原生 Langfuse grounding Evaluator；
- `assistant_agent.quality.response_quality`：复用原生 Langfuse response quality Evaluator；
- `assistant_agent.quality.tool_result_quality`：仅在 Staging 的真实 Tool observation 上复用。

`task_conformance` 内部 assertion 至少覆盖 required/forbidden tool、参数、顺序和目标终态，并把带 label
的明细写入 comment 和风险报告。不为每个 assertion 新建 `release.*` Score。

Score metadata 用 `evaluation_mode=release_review`、`phase`、`release_id` 和 `evaluator_version` 与日常
`evaluation_mode=live_observation` 区分。复用质量语言不等于合并运行状态。

以下内容只进入 `infrastructure_status`，不得记录成质量 Score 的 false：

- Provider/MCP/Langfuse/Evaluator 不可用或超时；
- Dataset、Trace 或 Score 未落库；
- catalog generation 不匹配；
- staging cleanup 失败。

Agent 已作出正确选择但真实依赖业务失败时，保留 `task_conformance` 的细分 assertion 和
`failure_owner=dependency`，使人能区分 Agent 回归与依赖故障。

## 8. 基线、报告与人工决定

每次评审先按绝对场景契约判定，再与最近一次人工批准 Run 做差异比较。只有 scenario hash、catalog
generation、Prompt contract 和 Evaluator version 一致时才做逐 Item 趋势结论；否则报告为不可直接
比较，但绝对契约结果仍有效。

报告包含：

- release/model/prompt/Git/catalog/evaluator 标识；
- Critical、High、flaky 和 infrastructure 风险；
- 每项风险对应的 Langfuse Experiment Item、Trace、工具调用和 Score；
- 相对批准基线的变化；
- staging 写入与 cleanup 结果。

人工决定为 `approved`、`approved_with_risk` 或 `rejected`。该决定不是质量 Score。决策记录只保存
release ID、Experiment Run ID、版本、风险摘要、操作人、时间和说明，不复制 Provider 原始输出或
真实 Trace。

## 9. 十分钟预算

- preflight 与 Dataset sync：最多 30 秒；
- Decision：最多 3 分钟，并发 4；
- Staging：最多 5 分钟，并发 2；
- Evaluator 落库、汇总和报告：最多 1 分钟；
- 全局硬超时：9 分 30 秒，保留 30 秒给停止与资源记录。

单 Item 超时记录 infrastructure error，其余 Item 继续。全局超时后必须停止新调用、记录已创建的
staging 资源并进入 cleanup，不得生成虚假的“通过”报告。

## 10. 代码结构与迁移

新增：

```text
evals/release_review/
  contracts.py
  loader.py
  catalog.py
  decision_backend.py
  staging_backend.py
  assertions.py
  experiment.py
  report.py
  sync_dataset.py
  scenarios/*.yaml
scripts/run_release_review.py
```

新链完成一次真实预发布验证后，删除：

- `evals/agent/` 旧 Task/Mission、Environment、Grader、calibration、overlay 和 backend；
- `scripts/run_agent_evals.py` 与旧同步入口；
- 只服务旧 eval 的 Runtime `registry_transform`；
- 旧 Agent eval TDD 测试；
- 旧 Remote Experiment service/route 实现，并由 Release Review service/route 替代；
- 旧 Agent Experiment 文档、authority 路由和配置。

保留并改造 webhook proxy、Langfuse/OTel 基础设施与 canonical Evaluator。日常 Trace、Live Rule、runtime
audit 和 production Runtime 默认执行路径必须保持兼容。

迁移按“新链并行落地 -> 四个代表场景转 YAML -> 离线验证 -> 单次真实预发布 Experiment -> 删除旧链”
完成，不长期维护双轨兼容。

## 11. 测试与验收

pytest 只验证 YAML schema、loader、assertion、Decision fixture backend、风险归因、报告、webhook 安全和
Langfuse fake client，不调用真实 Provider。真实 Provider、Tool catalog、MCP、写入、Langfuse 原生
Experiment/Evaluator 和 Run Comparison 只由显式预发布 Release Review 验证。

验收条件：

- 一个 Dataset 和一个 Experiment Run 同时包含 Decision 与 Staging Item；
- 完整运行小于十分钟；
- 漏调、误调、参数错误和调用顺序错误可被确定性区分；
- Decision 不产生真实外部副作用；
- Staging 只使用独立预发布资源并报告 cleanup；
- 三个 task-level canonical Score 实际落库，真实 Tool observation 可保留 tool result quality；
- 基础设施故障不污染质量 Score；
- Langfuse 可原生比较当前 Run 与最近批准 Run；
- 人工决定及其证据引用可追溯；
- 日常评分和 runtime audit 行为不变。

## 12. 非目标

- 不为普通代码提交建立自动 CI 门禁；
- 不自动批准或拒绝发布；
- 不用生产用户账号或数据做 Staging；
- 不把日常 Trace audit 合入 Release Review；
- 不用关键词、正则或 capability 规则替 Agent 选择工具；
- 不用单一总分隐藏关键失败；
- 不在 Dataset、报告或 Git 中保存凭据、Provider 原始响应或真实用户数据。
