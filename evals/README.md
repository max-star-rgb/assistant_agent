# Eval 体系

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | system eval 与 Agent Task Experiment 的当前权威 |
| Owns | eval 分层、Task/Environment、Dataset 同步、Experiment、Remote webhook、task-level Score 完整性 |
| Does not own | 生产 Trace 生命周期、日常 runtime audit、Live Observation Rule 配置 |
| 源码与 schema 入口 | `evals/agent/`、`src/assistant_agent/evaluation/`、`src/assistant_agent/api/routes_eval_experiments.py` |
| 验证入口 | `docs/authority.toml` 中 `agent-eval.verification` |
| 相邻 authority | Runtime trace 与日常评分见 [`../docs/observability-harness.md`](../docs/observability-harness.md)；pytest 归属见 [`../tests/README.md`](../tests/README.md) |

`evals/` 回答真实能力是否连通、具体实现专项风险是否仍可复现，以及 Agent 在完整任务中的行为是否
合格。永久核心代码契约属于 `tests/core`；临时功能 TDD 属于 `tests/tdd/<feature>`。

```text
evals/
  system/
    <domain>/              # 正式真实 Provider/Tool/Context/Memory 连通性
    incubating/<feature>/  # 显式运行、可整目录删除的节点/实现专项检查
  agent/                   # Task 中心的端到端 Agent 行为评测
```

## 边界

| 层 | 回答的问题 | 结果权威 |
| --- | --- | --- |
| `tests/core` | 已登记的稳定核心不变量是否正确 | 核心测试代码与默认 pytest 结果 |
| `tests/tdd/<feature>` | 功能实现期间的临时 RED/GREEN 是否成立 | feature 测试代码与显式 pytest 结果 |
| `evals/system/incubating` | 有风险证据的节点或实现专项事实是否仍成立 | feature README、`checks_*.py` 与显式运行结果 |
| 正式 `evals/system` | 真实外部能力是否连通并经过治理链路 | 本地 runner 与 `.data/evals/system/` artifact |
| `evals/agent` | Agent 能否在受控任务中作出正确决策并完成目标 | Git Task/Grader 与 Langfuse Experiment/Score |

不要用 mock fallback、目录混放或重复 runner 让一层伪装成另一层。

### 文档权威与维护边界

本文件是 eval 分类、Agent Experiment、Dataset 同步、Remote Experiment webhook 和 Score 完整性
契约的唯一操作权威。相邻文档只承担以下职责：

- `docs/observability-harness.md` 定义 Runtime trace、日常审计和 Live Observation Rule；只说明它们
  不负责 Experiment，并链接到本文件；
- `scripts/README.md` 只索引稳定命令，说明用途、主要副作用和权威文档入口；不复制运行顺序、环境变量、
  webhook 请求体或 UI 字段；
- `.codex/skills/langfuse-eval-engineering/SKILL.md` 是执行检查清单，不是事实权威，开始工作时必须先读
  本文件。

维护时先用源码和测试确认行为，再只修改拥有该契约的权威章节；其他入口原则上只检查链接和一句话边界，
不得同步复制正文。涉及 eval、runtime audit 或二者边界的文档变更，完成前先运行 authority validator，
再按需运行完整文档证据扫描：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root .
```

validator 负责 owner、路由、精确事实和源码复核提示；collector 负责链接与文档库存。两者都不能判断
自然语言语义是否重复，review 仍须检查薄入口没有重新展开本文件正文。

## Incubating system checks

`evals/system/incubating/<feature>/` 只在存在明确风险证据和持续观察价值时，保存具体 node、
Provider adapter 或实现专项检查。它不属于默认 pytest、发布门禁或正式真实 system eval。每个目录
必须自包含，并说明 scope、mode、显式命令、副作用门禁、删除条件和晋升路径。

`checks_*.py` 保持 mock/local/offline，不读取真实 `.env` 或调用真实 Provider。对应事实由正式
system runner、Agent eval Experiment 或生产证据稳定覆盖后，可以手动删除整个 feature 目录，不得
因此修改 `tests/core`。真实连通性必须晋升到下述正式 system eval，使用 runner、artifact、real mode、
完整配置和 operator 显式确认。

## System eval

所有涉及真实 Provider 或外部 Tool 的运行必须显式设置
`MULTIMODAL_AGENT_PROVIDER_MODE=real`，完整配置所需 Provider，并由 operator 传入对应确认参数；
不能因检测到 key 自动运行，也不能从 real 静默回退 mock。纯本地 system eval 仍需独立的 operator
副作用确认，但不伪装成 Provider real mode。

Tool 连通性：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py \
  --allow-real-tools \
  --case-id weather_beijing_today

MULTIMODAL_AGENT_SHOPPING_PROVIDER=haodanku \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py \
  --allow-real-tools \
  --case-id shopping_search_real_single_need

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_shopping_eval.py \
  --allow-real-tools \
  --keyword 纸巾

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_create_eval.py \
  --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_create_eval.py

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_search_eval.py \
  --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_calendar_search_eval.py
```

通用 Tool eval 结果写入 `.data/evals/system/tools/<run>/`。其中购物直连命令不经过 LLM，
直接通过 `ActionValidator -> ToolExecutor -> ToolRegistry -> shopping_search` 验证好单库；
它要求至少一个 `source=haodanku` 的真实候选、正价格和 HTTP(S) 购买链接，结果写入
`.data/evals/system/shopping/<run>/`。

两个本地日历命令都不经过 LLM、`AgentGraphRuntime` 或 assistant loop，分别保留
`ActionValidator -> ToolExecutor -> ToolRegistry -> calendar_create|calendar_search` 治理链。
它们使用 operation-scoped 真实 SQLite，不读取 `.env`、不访问网络，也不要求
`MULTIMODAL_AGENT_PROVIDER_MODE=real`。`calendar_create` 验证首次提交、幂等回放和数据库终态；
`calendar_search` 先通过 adapter 向隔离数据库预置一条合成事件，再只执行一次搜索 Tool，并验证
搜索前后 snapshot 不变。预置动作不计作被测 Tool call。

数据库、summary 和完整结构化结果分别保留在
`.data/evals/system/tools/calendar/create/<run>/` 和
`.data/evals/system/tools/calendar/search/<run>/`。IDE 可以直接运行
`evals/system/tools/calendar_create.py` 或 `calendar_search.py`，不需要配置参数；无参数默认执行
隔离的真实 SQLite eval，并先输出 `status=running`，结束时再输出完整检查结果。需要只查看输入和
目标路径时显式使用 `--dry-run`。

Context 捕获：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_context_eval.py \
  --allow-unredacted-context
```

仅允许合成输入，结果写入 `.data/evals/system/context/<run>/`，不得提交。Memory 当前没有完整
delete/reset 公共契约，因此不提供会写入真实 Mem0 的自动 runner；详见
`evals/system/memory/README.md`。

本地 SigLIP2 联合 image/text system eval 先用
`scripts/run_system_multimodal_embedding_eval.py --dry-run` 检查配置；只有 operator 显式传入
`--allow-local-model` 才创建 CUDA session。artifact 不保存向量、文本、图片内容或媒体路径，具体见
`evals/system/multimodal_embedding/README.md`。dry-run 会声明固定 5 FPS、latest-wins、纯语义选帧、
VLM 文本索引与查询阶段不调用 VLM 等检查面；它不等于真实 CUDA 或端到端流水线执行结果。

## Agent eval

Agent eval 把确定性任务符合度与语义质量分开：Git/Environment 只计算
`task_conformance`，Langfuse 原生 Evaluator family 同时负责日常 Trace 与 Experiment 的
`grounding/response_quality`。对外固定保留三个 task-level canonical Score：

```text
Task (用户挑战)
  + Environment (runtime、工具、依赖、状态与隔离)
  -> AgentGraphRuntime
  -> Evidence (轨迹、终态、状态与回答)
  -> Git Rule: task_conformance
  + Langfuse UI-selected Experiment Evaluators: grounding / response_quality
  -> 三个 task-level Score + 可选 live observation Score
  -> Langfuse Experiment
```

Git 是回归定义权威：

```text
evals/agent/
  contracts.py          # Task、Evidence、Grader 契约
  environment_base.py   # production Registry、精确替换和 Environment 生命周期
  loader.py             # Task、Suite 和入口加载
  evidence.py           # Runtime Trace 的稳定投影
  grading.py            # Environment outcome 与 Mission objective Rule
  provider_gate.py      # real 模式与 Provider 完整性闸门
  calibration.py        # 人工正反标签与原生 Evaluator 校准投影
  langfuse_backend.py   # Dataset 发布与 Experiment 薄适配
  cli.py                # 稳定命令入口
  suites.json           # Mission ID 集合
  missions/<mission_id>/
    task.json           # 用户请求、capability 与代码入口
    environment.py      # replacement、状态证据和执行 hook
    calibration.json    # 人工标注的正反证据
```

当前基础 Task 案例已删除，只保留 Mission。Mission 必须由结构化状态 Evidence 证明客观终态，
Environment 必须实现非空、只含 Rule assertion 的 `objective_state_assertions()`；目标状态 Rule 只由
Environment 拥有。

Langfuse 是协作和运行后端：Dataset 保存已发布请求，Experiment 保存 Trace、输出和 Score。Dataset
item 只包含 `task_id + request + 短 metadata`，不复制 case level、Environment、state oracle、
Evaluator prompt、长依赖说明或其他 oracle，也不能把 Langfuse Dataset 当作回归定义的唯一副本。

### Mission 规则

- 一个 Mission 只验证一个可命名 capability；
- `task.json` 的请求必须像自然用户请求，不描述测试机关；
- Environment 使用活动 `AgentGraphRuntime` 和 production Tool Registry；replacement 只能精确替换
  production Registry 中已有的同名 Tool，且必须保持完整 ToolSpec，不得追加平行模拟目录；
- Environment 继承 `ControlledTaskEnvironment`：公共
  `describe/validate/tool_outcome_expectations/execute` 由共享模板拥有；案例只实现精确 replacement、
  必需成功/失败、Mission Rule 和状态 hook；
- 所有 Mission 默认使用相同配置下的完整 production Tool Registry，不按 Mission、capability、
  用户话术或目标工具裁剪目录。媒体、
  entry profile 和 durable ready-step 等运行时结构化条件仍可在具体 run 中收窄可见集合；
- 特殊场景需要改变目录时，Environment 或受信入口可以通过结构化
  `metadata.tool_visibility.profile + allowed_tools` 精确收窄工具集合。override 必须声明可读的
  profile，`allowed_tools` 必须是已注册 production 工具的子集，runtime validation 必须检查该配置，最终可见集合
  仍须完整声明 outcome expectation；不得把 override 放入自然语言或 Dataset metadata，
  也不得借此启用未配置或未授权的真实工具；
- Environment 为每个可见工具声明结果预期；目标工具可以是必调的 `must_succeed` 或
  `must_fail_with(error_code)`，其余正常目录工具可以声明为非必调、但一旦调用就必须成功。该声明
  不会进入 Agent input 或 Dataset metadata；
- real 模式服务启动即授权已配置 Tool 的真实调用与写入；正式运行不自动回滚。需要确定性状态的
  Mission 必须把对应 Tool 明确声明为 replacement，并在 Evidence 中记录来源；
- objective Rule 和人工标签对 Agent 隐藏；客观事实由 Git Rule 检查，开放语义由 Langfuse 原生
  Evaluator 判断；
- Experiment runner 固定输出 `assistant_agent.quality.task_conformance`、
  `assistant_agent.quality.grounding`、`assistant_agent.quality.response_quality` 三个彼此独立的 BOOLEAN
  task-level Score，不生成 reward 或总通过分；
- Environment oracle 直接生成 `task_conformance`；单个工具结果质量由
  `assistant_agent.quality.tool_result_quality` Live Observation evaluator 负责；
- `tool_execution` 同时表示工具 outcome 与 Environment oracle 匹配，并合入
  `objective_state_assertions()` 的终态 Rule；
- 不创建天气、日历、Workflow 等 capability 专属 Score；原生 `response_quality` 直接依据用户请求
  与最终回答判断完整性；
- 每条 Git assertion 必须标记 `evaluation_method=rule`；Langfuse Evaluator reasoning 保留在原生 Score；
- 每个案例至少保留一个正确样本和一个可信错误样本；校准文件统一经
  `load_calibration_set()` 按 `schema_version` 分派解析。

当前 Deep Research Mission：

- `deep_research_autonomous_admission`：复杂行业研究请求应由 ReAct LLM 自主调用
  `workflow_submit(workflow_type="deep_research")`，不增加关键词分类器或第二个 decision LLM；
- `deep_research_constraint_grounding`：中国市场、私有化部署、来源优先级、待确认边界和三类交付物
  必须进入持久 Workflow state；
- `deep_research_evidence_plan`：面对冲突证据研究，必须初始化 scope、来源收集、证据抽取、大纲、
  草稿、核验和合成七阶段计划，并保留多来源目标。

三个 Mission 共用 `DeepResearchMissionEnvironment`：活动 `AgentGraphRuntime` 使用 production Tool
Registry，并只对案例明确声明的同名工具应用原子 replacement；未替换工具保持部署中的真实依赖。
其 suite 名为
`deep_research`；`--inspect --suite deep_research` 离线，正式 `--run` 才调用真实 Agent Provider 和
Langfuse 原生 Evaluator。

`evals/agent/tasks/` 下的基础能力案例已移除。Environment 的静态检查不会提前构建真实 Registry；
正式执行时由 Runtime 装配 production Registry，再执行同名 replacement 和完整校验。Evidence 会记录
每次工具执行来自 `live` 依赖还是 `controlled_replacement`，避免评测侧追加模拟工具造成重名注册。

### 正式运行顺序：PyCharm + Dataset 同步 + Langfuse UI

1. 检查 Mission 和 Environment，不联网：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --inspect \
  --task deep_research_autonomous_admission
```

`--inspect` 会显示 `case_source.level`，并显示 `mission_objective_rule.required/implemented`，以便在
同步前发现 Mission 缺少目标状态 Rule。

2. 按[“从 Langfuse UI 触发 CLI”](#从-langfuse-ui-触发-cli)完成一次性 webhook 配置，然后在
PyCharm 选择共享 Run Configuration **Langfuse** 并点击 Run。它执行
`scripts/run_langfuse.py`，启动本地 Compose、等待 `http://localhost:3000` 健康并保持前台；点击 Stop
只停止容器，不删除数据。Agent Experiment 还要求 **Assistant Server** 以 real Provider mode 运行，且
Remote Experiment 开关、签名 secret 和 Dataset 已配置。

3. 直接运行 `evals/agent/sync_langfuse_dataset.py`（IDE 中右键 Run 即可）：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/agent/sync_langfuse_dataset.py
```

默认 Dataset 为 `assistant-agent-regression`。同步会 upsert Git 中全部 Mission，并删除只属于该
Dataset、但本地已不存在的陈旧 Git-owned item；它不运行 Agent 或 Evaluator。
原生 item ID 使用 `<dataset_name>__<task_id>`，避免与同一 Langfuse project 的历史 Dataset item
冲突。
在第一次 Langfuse 写入前，发布入口会重新加载并验证 Git case 的 Environment 和
calibration；Mission 还必须提供可执行、非空且仅含 Rule 的 `objective_state_assertions()`。
任一契约缺失时发布直接按基础设施错误失败，不会留下“已 ACTIVE 但不可运行”的新 Dataset item。

如需暂时保留陈旧 item，可显式传入 `--keep-stale`。

4. 打开 Langfuse UI：`Datasets -> assistant-agent-regression`，通过 Dataset Items 的 ACTIVE/ARCHIVED
状态选择本次范围。按下文的 UI 字段表启动单 Mission 烟测，并选择
`assistant_agent.quality.grounding` 与 `assistant_agent.quality.response_quality` 两个 Experiment
Evaluator。UI webhook 只负责触发同一个受治理的 `run_agent_evals.py --run`，不会把 Agent loop 搬进
Langfuse。

5. 确认单 Mission 的 Trace、三个 canonical Score 与 Environment 终态后，再按下文 suite config 扩大
范围。第一次运行不得使用空 config，因为 `{}` 会运行 Dataset 中全部 ACTIVE Git Mission。

Agent 使用仓库显式配置的真实 Chat Provider；语义评分使用 Langfuse UI 为 Experiment 选择的 Evaluator
和 LLM connection，两者不复用同一 Provider adapter。日常 Trace 的 Live Observation Rule 由
runtime-audit 配置入口管理，但与 Experiment 的 Dataset 同步、Evaluator 选择和 UI Run 无关。缺失
Experiment Score 时，后台 CLI 按评测基础设施失败退出 2，不写成 `false`。

### 安全和退出码

- `--inspect` 不读取 `.env`，不联网；
- `--publish` 需要 Langfuse 凭据，但不调用 Chat Provider；
- `--calibrate` 要求 `--allow-real-provider`，用于确认 Langfuse 原生 Judge 的真实费用；它不启动
  Agent Chat Provider；`--run` 还要求 `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和完整真实 Chat 配置；
- `--run` 还要求 Langfuse 凭据与可用的 OTLP Trace 导出；
- `--run` 使用 production Registry 和已配置真实 Tool，可能产生外部调用与持久化写入；未声明
  replacement 的 Tool 不会静默回退到 mock，也不会自动清理副作用；
- `--run` 在完整产出三个 task-level Score 后返回 0，不再根据分数组合返回 Agent 失败码；
- `--calibrate` 将正反 Evidence 发布到 `assistant-agent-evaluator-calibration` Dataset，并比较三个
  canonical Score 与人工标签；不一致返回 1；
- 凭据、Environment、Mission Rule、Dataset、Trace、Evaluator、Evidence 或 Score 故障返回 2；
- 通过返回 0。

### 评分与基础设施

固定 task-level Score：

```text
assistant_agent.quality.task_conformance
assistant_agent.quality.grounding
assistant_agent.quality.response_quality
```

三个持久化 Score 全部采用阳性语义且不聚合：

- `task_conformance`：基础 Task 的实际工具终态是否符合 Environment
  oracle，Mission 还要求目标状态 Rule 通过。预期 `provider_timeout` 且实际错误码相同仍为 `true`；
- `grounding`：由 canonical Langfuse Evaluator 判断最终回答是否忠于工具结果和结构化终态；
- `response_quality`：由同一 canonical Evaluator family 判断回答是否回应用户请求且清晰完整。

因此天气超时恢复案例可以产生：

```text
task_conformance=true
tool_result_quality=false  # 由 tool.execute observation evaluator 产生时
grounding=true
response_quality=true|false
```

`task_conformance` 的 Git Rule 结果具有确定性权威，Langfuse Evaluator 不得覆盖。Evaluator Provider 超时、
输出不可解析或未返回 Score 属于评测基础设施失败，不得记录为 Agent Score 失败。

Score comment 在通过时展示 assertion label，失败时展示 label 与真实 reason。Langfuse Score
metadata 把每条 assertion 的 `passed`、
`label`、`method` 和可选 `criterion_id` 写成独立标量字段，避免把完整 rubric、reason 或嵌套大对象
传播成超长属性。

Experiment runner 的本地 evaluator 只写 `task_conformance`；Langfuse UI 为该 Experiment 选择的
Evaluator 异步写入 `grounding/response_quality`。后台 CLI `flush()` 后通过 Langfuse Scores v3 API
同时按 trace ID 与 observation ID 轮询。
三个 task-level Score 必须实际落库、名称无缺失或重复，并且
全部挂在该 item 的同一个 `experiment-item-task` observation 上；否则按评测基础设施失败退出 2，
不能因为 SDK 内存结果存在而报告成功。当前自托管 Langfuse 4.6 使用
`client.api.observations.get_many()`（Observations v2）；3.224.2 的 legacy observation API 不再是
运行兼容路径。

工具业务结果预期以 Environment 的强类型声明为唯一事实源：

```python
ToolOutcomeExpectation.must_succeed("weather")

ToolOutcomeExpectation.must_fail_with(
    "weather",
    error_code="provider_timeout",
)

ToolOutcomeExpectation(
    tool_name="calendar_search",
    required=False,
    expected_result="success",
)
```

校准和正式 Experiment 都通过 `grade_task_conformance()` 比较实际 `tool.finished/tool.failed` 与
Environment oracle；Mission 在同一维度合入目标状态 Rule。Task 不再创建专属 grader 或
`response_quality` rubric。

Calibration v3 仍作为迁移期人工标签格式读取；原生校准只投影其中的 `tool_execution ->
task_conformance`、`grounding` 和 `response_quality`，不再调用旧 `tool_semantics` Judge。

Environment validation、凭据、Dataset、Trace 导出、Evidence 解析和 Evaluator 故障属于评测基础设施
失败，退出 2，不生成或篡改 Agent Score。

### 迁移兼容与删除计划

正式 `--run/--calibrate/--publish` 已不再引用本地 Provider Judge。为读取既有 Task 与 Calibration v3，
`judge.py`、`batch_grading.py`、旧 `grade_task()` / `run_calibration()`、历史 `grader.py` 和
`tool_semantics/judge_verdicts` 字段暂时保留。新 Task 禁止继续使用这些入口；待历史 Task 定义和校准
fixture 完成字段迁移后，应在一次独立清理中删除兼容层及其旧专项测试，避免长期维护两套评分机制。

### 从 Langfuse UI 触发 CLI

本节是 Remote Experiment 环境变量、请求体、UI 字段和操作顺序的唯一权威；其他仓库文档只能链接本节。

Assistant Server 提供默认关闭的 Remote Custom Experiment webhook：

```text
POST /internal/evals/langfuse/remote-experiment
```

它只负责验签、校验统一 Dataset 和 Git 中已有的 Task/Suite，然后在后台以固定 argv 启动
`scripts/run_agent_evals.py --run`。请求不能传入 shell、环境变量、env file、写权限或其他 CLI
参数；Task、Environment、Git Rule 和三个 task-level Score 使用上述统一定义。CLI stdout、stderr 和状态回执
写入 `.data/evals/remote/<trigger_id>.*`，不提交。

先在仓库根未跟踪 `.env` 中配置 Assistant Server：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=real
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_ENABLED=true
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_SIGNING_SECRET=<Langfuse-setup-secret>
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_DATASET=assistant-agent-regression
```

再在未跟踪的 `.data/langfuse/.env` 中配置 Compose 代理；签名 secret 必须与仓库根 `.env` 完全相同：

```text
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_SIGNING_SECRET=<同一个 Langfuse-setup-secret>
LANGFUSE_WEBHOOK_WHITELISTED_HOST=assistant-agent-eval-webhook
```

修改任一文件后都要重启 PyCharm 中的 **Langfuse** 与 **Assistant Server**。不要提交这两个 `.env`。

当前 Langfuse `4.6` 部署继续保留 Remote Experiment 代理作为端口、签名与 Assistant Server 安全边界。
本轮升级只验证了代理容器健康和 Compose 网络可达，没有在 real Provider 模式触发远程 Experiment；
因此请求体仍按现有兼容契约解析：

```json
{
  "projectId": "<project-id>",
  "datasetId": "<dataset-id>",
  "datasetName": "assistant-agent-regression",
  "payload": "{}"
}
```

`payload` 是 UI 中 Default config 的原始 JSON 字符串，不是顶层 `config` 对象。
Assistant Server 对其二次解析。空对象默认运行 Dataset 中全部 ACTIVE 且能映射到 Git 的 Task；
`task|suite + runName` 只作为精确调试时的高级白名单字段。

内部 80 端口代理会在请求没有 `x-langfuse-signature` 时使用同一个 secret 补充
`t=<timestamp>,v1=<hmac-sha256>`；Langfuse 已携带签名时，代理保持原值、不重复签名。

然后在 Langfuse Dataset 页面选择 `Run Experiment -> via Webhook -> Configure`：

| UI 字段 | 值 | 说明 |
| --- | --- | --- |
| Name | `assistant-agent-evals` | 仅显示名称，可以改成其他易识别名称 |
| URL | `http://assistant-agent-eval-webhook/internal/evals/langfuse/remote-experiment` | 使用 Compose 内部代理；禁止填写 `localhost:8089` 或 `host.docker.internal:8089` |
| Default config | `{}` | 原样成为请求顶层 `payload` 的 JSON 字符串 |
| Headers / Signing secret | 留空 | 内部代理为无签名请求补充共享 HMAC；若 UI 已发送签名，代理会原样保留 |
| Enabled | 开启 | 保存后才可用于 Dataset Experiment |

`projectId`、`datasetId` 和 `datasetName` 由 Langfuse 自动放入请求 envelope，不填写到 Default
config。配置保存后，先运行 `evals/agent/sync_langfuse_dataset.py`，再在 Dataset Items 中用
ACTIVE/ARCHIVED 控制范围并点击 `Run`。运行弹窗中的 Experiment Evaluator 选择不属于 webhook 配置；
应独立选择 `assistant_agent.quality.grounding` 与 `assistant_agent.quality.response_quality`。

日常使用不需要记忆字段。只有精确调试时才临时覆盖 config：

```json
{"task":"deep_research_autonomous_admission","runName":"ui-deep-research-admission"}
```

或：

```json
{"suite":"deep_research","runName":"deep-research-suite"}
```

首次使用建议先运行：

```json
{"task":"deep_research_autonomous_admission","runName":"deep-research-admission-first"}
```

空 config `{}` 会运行统一 Dataset 中全部 ACTIVE 且能映射到 Git 的 Task，只适合已经核对 ACTIVE 范围的
批量运行。

当前 Langfuse 4.6 的本机 Docker Compose 继续使用 `assistant-agent-eval-webhook` 在内部 80 端口提供
单路径代理，避免把 Assistant Server 的 8089 端口直接暴露给 Remote Experiment，
并设置 `LANGFUSE_WEBHOOK_WHITELISTED_HOST=assistant-agent-eval-webhook`；代理原样转发 body 和
已有的 `x-langfuse-signature`，或为未签名请求补签后，转发到绑定
`0.0.0.0:8089` 的 Assistant Server。不要在 UI URL 中填写
`host.docker.internal:8089`。

webhook 校验 HMAC SHA-256 和五分钟时效，对相同签名与 body 的重投递只启动一次；收到请求后立即
返回 `202 Accepted`，CLI 继续异步执行。只有显式启用 webhook、配置签名 secret 且 Server 运行于
real Provider mode 时才接受触发。

Assistant Server 控制台为 UI 触发的运行显示一个整体进度条。需要查询或停止后台运行时，在该控制台
输入：

```text
eval status
eval stop
eval status <trigger_id>
eval stop <trigger_id>
```

省略 `trigger_id` 时操作最近一次运行；`eval stop` 只会停止回执中保留了完整固定命令和独立进程组的
新版本运行。

真实运行生成的数据只保存在 Langfuse 和未跟踪 `.data/**`；不得提交凭据、原始生产 Trace、真实
用户数据或 Provider 原始响应。
