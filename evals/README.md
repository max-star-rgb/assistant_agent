# Eval 体系

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

Agent eval 内部 grader 采用四个分离维度；Langfuse 对外只写三个 task-level canonical Score，单工具
语义质量交给对应 observation evaluator：

```text
Task (用户挑战)
  + Environment (runtime、工具、依赖、状态与隔离)
  -> AgentGraphRuntime
  -> Evidence (轨迹、终态、状态与回答)
  -> Task-local Grader
  -> 三个 task-level Score + 可选 observation Score
  -> Langfuse Experiment
```

Git 是回归定义权威：

```text
evals/agent/
  contracts.py          # Task、Evidence、Grader 契约
  environment_base.py   # 受控 Environment 的共享生命周期与工具可见性
  batch_grading.py      # 固定三项 Judge 与 Task rubric 工厂
  loader.py             # Task、Suite 和入口加载
  evidence.py           # Runtime Trace 的稳定投影
  grading.py            # 内部四维与 Environment outcome 匹配
  judge.py              # 真实 Provider 语义 Judge 边界
  provider_gate.py      # real 模式与 Provider 完整性闸门
  calibration.py        # 正反 Evidence 直接校准
  langfuse_backend.py   # Dataset 发布与 Experiment 薄适配
  cli.py                # 稳定命令入口
  suites.json           # Task ID 集合
  tasks/<task_id>/
    task.json           # 用户请求、capability 与代码入口
    environment.py      # 依赖、工具、状态、隔离与执行
    grader.py           # 隐藏评分逻辑
    calibration.json    # 人工标注的正反证据
  missions/<mission_id>/ # 与 Task 相同的运行协议；额外验证目标终态
```

`tasks/` 与 `missions/` 只是案例组织层级，共用 loader、Environment、Evidence、校准、发布和运行
协议；loader 会拒绝两个目录之间重复的 ID。基础 Task 只验证受控工具 outcome；Mission 适用于还必须
由结构化状态 Evidence 证明客观终态的案例。Mission Environment 必须实现非空、只含 Rule assertion 的
`objective_state_assertions()`；目标状态 Rule 由 Environment 拥有，Task-local grader 不拥有该 Rule。

Langfuse 是协作和运行后端：Dataset 保存已发布请求，Experiment 保存 Trace、输出和 Score。Dataset
item 只包含 `task_id + request + 短 metadata`，不复制 case level、Environment、state oracle、grader
rubric、长依赖说明或其他 oracle，也不能把 Langfuse Dataset 当作回归定义的唯一副本。

### Task 规则

- 一个 Task 只验证一个可命名 capability；
- `task.json` 的请求必须像自然用户请求，不描述测试机关；
- Environment 使用活动 `AgentGraphRuntime`，可以模拟依赖，不能模拟 Agent 决策，并在运行前验证
  Tool Registry、受控依赖、隔离和复位前提；
- 基础 Task Environment 继承 `ControlledTaskEnvironment`：公共
  `describe/validate/tool_outcome_expectations/execute` 由共享模板拥有；任务文件只实现受控依赖、
  registry replacement、必需成功/失败、Task 专属 Rule 和状态 hook；
- 所有 Task 的默认 Environment 使用同一份完整 Agent eval Tool Registry，不按 Task、capability、
  用户话术或目标工具裁剪目录；它包含 Agent 在相同结构化运行条件下会暴露的全部工具。媒体、
  entry profile 和 durable ready-step 等运行时结构化条件仍可在具体 run 中收窄可见集合；
- 特殊场景需要改变目录时，Environment 或受信入口可以通过结构化
  `metadata.tool_visibility.profile + allowed_tools` 精确收窄工具集合。override 必须声明可读的
  profile，`allowed_tools` 必须是已注册受控工具的子集，`validate()` 必须检查该配置，最终可见集合
  仍须完整声明 outcome expectation；不得把 override 放入自然语言、grader、Dataset metadata，
  也不得借此启用未配置或未授权的真实工具；
- Environment 为每个可见工具声明结果预期；目标工具可以是必调的 `must_succeed` 或
  `must_fail_with(error_code)`，其余正常目录工具可以声明为非必调、但一旦调用就必须成功。该声明
  不会进入 Agent input 或 Dataset metadata；
- 写操作必须使用每次运行可丢弃或可复位的状态；
- grader 对 Agent 隐藏，客观事实优先用代码检查，开放语义才用 Judge；
- Experiment runner 固定输出 `assistant_agent.quality.task_conformance`、
  `assistant_agent.quality.grounding`、`assistant_agent.quality.response_quality` 三个彼此独立的 BOOLEAN
  task-level Score，不生成 reward 或总通过分；
- 内部 `tool_execution` 由 Environment oracle 做 Rule 判定并映射为 `task_conformance`；内部
  `tool_semantics` 继续用于 grader 校准，但不写成 task-level Score；单个工具结果质量由
  `assistant_agent.quality.tool_result_quality` observation evaluator 负责；
- 对基础 Task，`tool_execution` 只表示工具 outcome 与 Environment oracle 匹配；对 Mission，它还必须
  合入 `objective_state_assertions()` 的终态 Rule；
- Task 专属要求只进入 `response_quality` rubric，并通过
  `grader_for_response_quality()` 绑定共享评分管线，不创建天气、日历等工具专属 Score；
- 每条 assertion 必须标记 `evaluation_method=rule|judge`；可客观证明的事实使用 Rule，开放语义才使用
  LLM Judge；
- grader 必须先通过至少一个正确样本和一个可信错误样本的直接校准；校准文件统一经
  `load_calibration_set()` 按 `schema_version` 分派解析。

当前天气 Task：

- `amap_weather_forecast_date_grounding`：调用
  `mcp.amap_maps.maps_weather(city="上海市")`，根据返回项的明确 `date` 选择明天白天预报，
  区分昼夜字段，并避免把日级预报说成精确小时预报；
- `amap_weather_missing_city_clarification`：用户没有提供城市或区县时先澄清，不猜测地点，也不提前
  调用天气工具；
- `amap_weather_provider_failure_recovery`：高德天气固定返回 `provider_timeout` 后，诚实说明当前
  没有可核实预报，不编造天气，并给出重试、出发前复查和有限的保守建议。此时
  内部 grader 的 `tool_execution=true`、`tool_semantics=false` 是合法组合；持久化时前者映射为
  `task_conformance=true`，后者不再写成 task-level Score。

当前旅行 Skill Task：

- `travel_skill_proactive_loading`：面对一个只需住宿搜索即可完成的简单旅行请求，Agent 仍须成功
  调用 `load_skill` 加载 `travel-tool-orchestration`，并调用 `lodging_search`。Environment 使用完整
  受控工具目录和确定性住宿依赖；校准反例保留正确住宿回答但省略 Skill 加载，使
  `tool_execution=false`，从而把内部工作流加载与回答质量分开判断。正式 Experiment 还应在 Trace
  中确认 `load_skill` 发生在住宿业务工具之前。
- `travel_itinerary_planning`：面对包含三位成人住宿、预算、抵离时间、父母慢节奏和三个必去地点的
  四日旅行请求，Agent 必须加载旅行 Skill，比较受控酒店报价与关键通勤，再生成按天、按时间段的
  可执行行程。Environment 提供确定性住宿、地理编码和公交路线证据；grader 同时检查抵返缓冲、
  OTA 边界，以及没有可识别网页或远期天气证据时是否把开放、预约和天气诚实列为待确认。

当前网页证据 Task：

- `website_unverified_url_honesty`：受控 website backend 对未声明 URL 返回
  `mock_url_unverified` 且不提供 `final_url` 后，Agent 必须说明没有可核实的页面证据，不编造页面标题、
  资格条件或办理步骤，并给出核对 URL、稍后重试或由用户提供页面内容等有限恢复建议。

每个 Agent Task Environment 的默认完整目录由共享 `build_controlled_registry()` 装配，包含 Agent
默认内置工具和与部署 allowlist 一致的 9 个高德 MCP namespaced 只读工具，不按 Task 选择子集。
目标工具连接该 Task 的确定性 runner，其余工具连接受控的本地或 mock 实现；整个 pytest/校准
Environment 都不连接真实高德服务，并使用每次运行隔离的 in-memory 状态。三个天气 Task 的目标
工具均为 `mcp.amap_maps.maps_weather`，输入使用真实 `city` schema。

### 运行顺序

1. 检查 Task 和 Environment，不联网：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --inspect \
  --task amap_weather_provider_failure_recovery
```

`--inspect` 会显示 `case_source.level`，并显示 `mission_objective_rule.required/implemented`，以便在
校准前发现 Mission 缺少目标状态 Rule。inspect、calibrate、publish、run 和 Scores v3 审计顺序保持不变。

2. 显式运行迁移后的 Agent eval infrastructure 专项检查（非 core、非发布门禁）：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  evals/system/incubating/agent-eval-infrastructure/checks_*.py
```

3. 使用真实 Judge 校准人工标注 Evidence：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --calibrate \
  --task amap_weather_provider_failure_recovery \
  --allow-real-provider
```

4. 显式发布 Task 到统一 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --publish \
  --task amap_weather_provider_failure_recovery
```

默认 Dataset 为 `assistant-agent-regression`。发布只 upsert 所选 Task，不运行 Agent 或 Judge。
原生 item ID 使用 `<dataset_name>__<task_id>`，避免与同一 Langfuse project 的历史 Dataset item
冲突。
在第一次 Langfuse 写入前，发布入口会重新加载并验证 Git case 的 Environment、grader 和
calibration；Mission 还必须提供可执行、非空且仅含 Rule 的 `objective_state_assertions()`。
任一契约缺失时发布直接按基础设施错误失败，不会留下“已 ACTIVE 但不可运行”的新 Dataset item。

需要把 Git 中全部 Task 与 Mission 一次性同步到统一 Dataset 时，可以直接在 IDE 中运行
`evals/agent/sync_langfuse_dataset.py`，无需传入参数；它会发布两个案例目录中的全部本地定义，并
删除只属于该 Dataset、但本地案例已不存在的陈旧 item。该入口只同步 Dataset，不启动 Experiment。
如需暂时保留陈旧 item，可在命令行显式传入 `--keep-stale`。

5. 运行一个 Task：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --task amap_weather_provider_failure_recovery \
  --allow-real-provider \
  --judge-timeout-seconds 30 \
  --judge-max-retries 0 \
  --run-name amap-weather-provider-failure-recovery
```

受控天气成功路径使用同一入口显式选择：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --task amap_weather_forecast_date_grounding \
  --allow-real-provider \
  --run-name amap-weather-forecast-date-grounding
```

6. 运行 Suite：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --suite release \
  --allow-real-provider \
  --run-name agent-release
```

`--task` 可重复，用于精确运行多个 Task；`--task` 与 `--suite` 互斥。三个高德天气基础 Task 已加入
`readonly` 和 `release`；`smoke` 仍保持最小快速案例。加入 suite 只表示 Git 定义和离线校准完整，
首次真实 Experiment 仍应先按 Task ID 运行并审计 Judge 与 Trace。
精确 Task 和 Suite 运行也只选择 ACTIVE Dataset item，不会重新执行 ARCHIVED 历史项；同一
`task_id` 若存在多个 ACTIVE item，运行会按基础设施错误 fail-fast，必须先在 Dataset Items 中只保留
一个 ACTIVE item。

运行统一 Dataset 中全部 ACTIVE Task：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --dataset-active \
  --allow-real-provider \
  --run-name dataset-active
```

`--dataset-active` 只用于 `--run`：它读取 Langfuse Dataset，忽略 ARCHIVED items，并要求每个 ACTIVE
item 的 `input.task_id`、`metadata.task_id` 与 Git Task 一致。空集合、未知 Task 或契约不一致都属于
评测基础设施错误，不会静默跳过。

Agent 与 LLM Judge 共享已显式选择的真实 Chat Provider 和模型，但不共享传输策略。Judge 固定
`stream=false`，Qwen Judge 关闭 thinking，并使用独立超时和 SDK 重试：

```text
AGENT_EVAL_JUDGE_TIMEOUT_SECONDS=30
AGENT_EVAL_JUDGE_MAX_RETRIES=0
AGENT_EVAL_JUDGE_NETWORK_MODE=ipv4_direct
```

命令行 `--judge-timeout-seconds`、`--judge-max-retries`、`--judge-network-mode` 优先于同名
环境变量。网络模式默认 `ipv4_direct`，绕过环境代理并把 Judge HTTP transport 绑定到 IPv4；
Provider 只能通过代理访问时，可显式改用 `environment`，沿用系统 HTTP(S) proxy 和 DNS。该开关
只改变 Judge 网络链路，不改变 Agent Provider 链路。
运行进度以逐行 JSON 写入 stderr，最终结果仍只写 stdout；每个 criterion 在 Langfuse 中生成
`judge.<criterion_id>` evaluator observation，其 input 保存当次实际使用的 `criterion_id`、
`rubric`、`task_id` 和 `run_id`。Judge 超时或连接失败仍属于评测基础设施失败，退出 2，不生成
Agent 失败分数。Langfuse SDK 即使内部捕获 evaluator 异常，CLI 也会重新抛出原始 Judge 故障，
不会用缺少 Score 等二次错误覆盖根因。`max_retries=0` 不会重试瞬时连接失败；需要由
operator 显式接受重试时，可把 `--judge-max-retries` 调为正数。

### 安全和退出码

- `--inspect` 不读取 `.env`，不联网；
- `--publish` 需要 Langfuse 凭据，但不调用 Chat Provider；
- `--calibrate` 和 `--run` 同时要求 `--allow-real-provider`、
  `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和完整真实 Chat 配置；
- `--run` 还要求 Langfuse 凭据与可用的 OTLP Trace 导出；
- `--run` 在完整产出三个 task-level Score 后返回 0，不再根据分数组合返回 Agent 失败码；
- `--calibrate` 仍校准内部四维，人工标注与实际内部维度不一致时返回 1；
- 凭据、Environment、Mission Rule、Dataset、Trace、Judge、Evidence 或 Score 故障返回 2；
- 通过返回 0。

### 评分与基础设施

固定 task-level Score：

```text
assistant_agent.quality.task_conformance
assistant_agent.quality.grounding
assistant_agent.quality.response_quality
```

三个持久化 Score 全部采用阳性语义且不聚合；内部 grader 仍保留四维以便校准：

- `task_conformance`：由内部 `tool_execution` 映射；基础 Task 的实际工具终态是否符合 Environment
  oracle，Mission 还要求目标状态 Rule 通过。预期 `provider_timeout` 且实际错误码相同仍为 `true`；
- 内部 `tool_semantics`：继续判断整项 Evidence 中工具数据是否可用，用于校准和诊断，但不持久化为
  task-level Score。正式单工具质量使用 observation-level `tool_result_quality`；
- `grounding`：Agent 最终回答是否忠于工具结果，包括正确理解成功、失败和空结果；
- `response_quality`：回答是否真正回应当前用户请求，并且表达清晰、完整、有用。

因此天气超时恢复案例可以产生：

```text
task_conformance=true
tool_result_quality=false  # 由 tool.execute observation evaluator 产生时
grounding=true
response_quality=true|false
```

`tool_execution` 的 Rule 结果具有确定性权威，Judge 不得覆盖。Judge Provider 超时、输出不可解析、
criterion 缺失或未返回 verdict 属于评测基础设施失败，不得记录为 Agent Score 失败。三个 Judge
固定使用 `tool_semantics`、`grounding`、`response_quality` criterion；每个 criterion 在
`experiment-item-evaluation` 下形成独立 `judge.<criterion_id>` observation。

Score comment 在通过时展示 assertion label，失败时展示 label 与真实 reason。Langfuse Score
metadata 把每条 assertion 的 `passed`、
`label`、`method` 和可选 `criterion_id` 写成独立标量字段，避免把完整 rubric、reason 或嵌套大对象
传播成超长属性。

Experiment 完成后，CLI 先检查 SDK 返回的每个 item 都包含三个 canonical BOOLEAN Evaluation，再
`flush()` 并通过 Langfuse Scores v3 API 回查。三个 task-level Score 必须实际落库、名称无缺失或重复，并且
全部挂在该 item 的同一个 `experiment-item-task` observation 上；否则按评测基础设施失败退出 2，
不能因为 SDK 吞掉 Score 写入异常而报告运行成功。
本机 Langfuse `3.224.2` 仍处于 v3 write mode，定位该 task observation 必须使用 SDK 的
`api.legacy.observations_v1`；Observations v2 只在 v4 write mode 可用。Score 记录本身继续使用
Scores v3 API 审计，两者不能因版本号相似而绑定到同一 API 代际。

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

校准和 Langfuse Experiment 都通过通用 `grade_task()` 自动比较实际 `tool.finished/tool.failed` 与
Environment 的成功/失败及错误码 oracle，并把结果写入 `tool_execution`；Mission 还在同一维度合入
Environment 的目标状态 Rule。Task grader 不再硬编码调用次数、顺序、参数、状态、objective Rule 或总
通过逻辑，只提供 Task 专属 `response_quality` rubric；通用入口固定执行另外两个语义 Judge。

Calibration v3 为每个 Evidence 显式保存四项 `expected_dimensions`，以及三个 Judge criterion 的
人工 `judge_verdicts`。校准逐项比较，不计算聚合通过标记。

Environment validation、凭据、Dataset、Trace 导出、Evidence 解析和 Judge 故障属于评测基础设施
失败，退出 2，不生成或篡改 Agent Score。Task-local rubric 只解释 `response_quality`。

### 从 Langfuse UI 触发 CLI

Assistant Server 提供默认关闭的 Remote Custom Experiment webhook：

```text
POST /internal/evals/langfuse/remote-experiment
```

它只负责验签、校验统一 Dataset 和 Git 中已有的 Task/Suite，然后在后台以固定 argv 启动
`scripts/run_agent_evals.py --run`。请求不能传入 shell、环境变量、env file、写权限或其他 CLI
参数；Task、Environment、Grader 和三个 task-level Score 仍完全使用仓库中的定义。CLI stdout、stderr 和状态回执
写入 `.data/evals/remote/<trigger_id>.*`，不提交。

先在 Assistant Server 的本机未跟踪环境中配置：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=real
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_ENABLED=true
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_SIGNING_SECRET=<Langfuse-setup-secret>
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_DATASET=assistant-agent-regression
```

本机固定使用 Langfuse `3.224.2`。该版本发送的请求体为：

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

`3.224.2` 尚未把 Remote Experiment 原生签名功能发布到自托管镜像，因此内部 80 端口代理会在
请求没有 `x-langfuse-signature` 时使用同一个 secret 补充
`t=<timestamp>,v1=<hmac-sha256>`；未来 Langfuse 原生携带签名时，代理保持原值、不重复签名。
同一个 secret 必须同时配置到仓库根 `.env`（Assistant Server）与
`.data/langfuse/.env`（Compose 代理），两处文件都不得提交。

然后在 Langfuse Dataset 页面选择 `Run Experiment -> via Webhook -> Configure`：

1. URL 填
   `http://assistant-agent-eval-webhook/internal/evals/langfuse/remote-experiment`；
2. Default config 保持 `{}`；
3. 打开 Enabled，保存配置；
4. 通过 `--publish` 确保 Task 已存在于统一 Dataset；
5. 在 Dataset Items 中用 ACTIVE/ARCHIVED 控制是否参与运行，然后点击 `Run`。

日常使用不需要记忆字段。只有精确调试时才临时覆盖 config：

```json
{"task":"amap_weather_provider_failure_recovery","runName":"ui-amap-weather-timeout"}
```

或：

```json
{"suite":"release","runName":"ui-release"}
```

Langfuse `3.224.2` 的 webhook SSRF 校验只允许 URL 使用 80 或 443，host/IP whitelist 不会放行
8089。因此本机 Docker Compose 使用 `assistant-agent-eval-webhook` 在内部 80 端口提供单路径代理，
并设置 `LANGFUSE_WEBHOOK_WHITELISTED_HOST=assistant-agent-eval-webhook`；代理原样转发 body 和
已有的 `x-langfuse-signature`，或为 3.224.2 的未签名请求补签后，转发到绑定
`0.0.0.0:8089` 的 Assistant Server。不要在 UI URL 中填写
`host.docker.internal:8089`。

webhook 校验 HMAC SHA-256 和五分钟时效，对相同签名与 body 的重投递只启动一次；收到请求后立即
返回 `202 Accepted`，CLI 继续异步执行。只有显式启用 webhook、配置签名 secret 且 Server 运行于
real Provider mode 时才接受触发。

真实运行生成的数据只保存在 Langfuse 和未跟踪 `.data/**`；不得提交凭据、原始生产 Trace、真实
用户数据或 Provider 原始响应。
