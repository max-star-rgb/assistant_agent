# Eval 体系

`evals/` 回答真实能力是否连通、以及 Agent 在完整任务中的行为是否合格。确定性代码契约仍属于
`tests/`。

```text
evals/
  system/   # 真实 Provider/Tool/Context/Memory 连通性
  agent/    # Task 中心的端到端 Agent 行为评测
```

## 边界

| 层 | 回答的问题 | 结果权威 |
| --- | --- | --- |
| pytest | 确定性代码契约是否正确 | 测试代码与 pytest 结果 |
| `evals/system` | 真实外部能力是否连通并经过治理链路 | 本地 runner 与 artifact |
| `evals/agent` | Agent 能否在受控任务中作出正确决策并完成目标 | Git Task/Grader 与 Langfuse Experiment/Score |

不要用 mock fallback、目录混放或重复 runner 让一层伪装成另一层。

## System eval

所有真实运行必须显式设置 `MULTIMODAL_AGENT_PROVIDER_MODE=real`，完整配置所需 Provider，并由
operator 传入对应确认参数；不能因检测到 key 自动运行，也不能从 real 静默回退 mock。

Tool 连通性：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py --dry-run

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_tool_evals.py \
  --allow-real-tools \
  --case-id weather_beijing_today
```

结果写入 `.data/evals/system/tools/<run>/`。

Context 捕获：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_system_context_eval.py \
  --allow-unredacted-context
```

仅允许合成输入，结果写入 `.data/evals/system/context/<run>/`，不得提交。Memory 当前没有完整
delete/reset 公共契约，因此不提供会写入真实 Mem0 的自动 runner；详见
`evals/system/memory/README.md`。

## Agent eval

Agent eval 采用四个分离概念：

```text
Task (用户挑战)
  + Environment (runtime、工具、依赖、状态与隔离)
  -> AgentGraphRuntime
  -> Evidence (轨迹、终态、状态与回答)
  -> Task-local Grader
  -> 固定四维 + agent_eval.reward
  -> Langfuse Experiment
```

Git 是回归定义权威：

```text
evals/agent/
  contracts.py          # Task、Evidence、Grader 契约
  loader.py             # Task、Suite 和入口加载
  evidence.py           # Runtime Trace 的稳定投影
  grading.py            # 固定四维、Environment outcome 匹配与 reward 聚合
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
```

Langfuse 是协作和运行后端：Dataset 保存已发布请求，Experiment 保存 Trace、输出和 Score。不要把
Environment、grader rubric、长依赖说明或 oracle 塞进 Dataset metadata，也不要把 Langfuse
Dataset 当作回归定义的唯一副本。

### Task 规则

- 一个 Task 只验证一个可命名 capability；
- `task.json` 的请求必须像自然用户请求，不描述测试机关；
- Environment 使用活动 `AgentGraphRuntime`，可以模拟依赖，不能模拟 Agent 决策，并在运行前验证
  Tool Registry、受控依赖、隔离和复位前提；
- Environment 为每个可见工具声明结果预期；目标工具可以是必调的 `must_succeed` 或
  `must_fail_with(error_code)`，其余正常目录工具可以声明为非必调、但一旦调用就必须成功。该声明
  不会进入 Agent input 或 Dataset metadata；
- 写操作必须使用每次运行可丢弃或可复位的状态；
- grader 对 Agent 隐藏，客观事实优先用代码检查，开放语义才用 Judge；
- 每个 Task 只有一个主要分数 `agent_eval.reward`；
- Langfuse 固定输出 `tool_execution`、`tool_use`、`state`、`response` 四个诊断维度；
- Task 专属 assertion 只保存在维度详情中，不创建天气、日历等工具专属 Score；
- 每条 assertion 必须标记 `evaluation_method=rule|judge`；可客观证明的事实使用 Rule，开放语义才使用
  LLM Judge；
- grader 必须先通过至少一个正确样本和一个可信错误样本的直接校准。

当前天气 Task：

- `weather_timeout_recovery`：真实 Chat Agent 看到默认运行时完整工具目录并自行选择 weather；
  Environment 只把 weather 后端替换为固定 `provider_timeout`，验证单次调用和失败结果消费，不调用
  真实天气服务。
- `weather_live_outdoor_run`：真实 Chat Agent 同样看到默认运行时完整工具目录并自行选择显式配置的
  weather MCP；Environment 要求 weather 成功，验证上海次日参数和回答是否忠于动态天气 Evidence，
  会调用真实天气服务。

两者都只读并使用每次运行隔离的 in-memory 状态。

### 运行顺序

1. 检查 Task 和 Environment，不联网：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --inspect \
  --task weather_timeout_recovery
```

2. 跑离线框架契约：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/integration/eval
```

3. 使用真实 Judge 校准人工标注 Evidence：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --calibrate \
  --task weather_timeout_recovery \
  --allow-real-provider
```

4. 显式发布 Task 到统一 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --publish \
  --task weather_timeout_recovery
```

默认 Dataset 为 `assistant-agent-regression`。发布只 upsert 所选 Task，不运行 Agent 或 Judge。
原生 item ID 使用 `<dataset_name>__<task_id>`，避免与同一 Langfuse project 的历史 Dataset item
冲突。

5. 运行一个 Task：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --task weather_timeout_recovery \
  --allow-real-provider \
  --judge-timeout-seconds 30 \
  --judge-max-retries 0 \
  --run-name weather-timeout-recovery
```

真实天气成功路径使用同一入口显式选择：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_agent_evals.py \
  --run \
  --task weather_live_outdoor_run \
  --allow-real-provider \
  --run-name weather-live-outdoor-run
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

`--task` 可重复，用于精确运行多个 Task；`--task` 与 `--suite` 互斥。当前 `smoke`、`readonly` 和
`release` 都只含 `weather_timeout_recovery`。`weather_live_outdoor_run` 必须按 Task ID 单独运行，
并在真实 Judge 校准和 Experiment 审计通过后才能加入 Suite。

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
不会再用“缺少 `agent_eval.reward`”覆盖根因。`max_retries=0` 不会重试瞬时连接失败；需要由
operator 显式接受重试时，可把 `--judge-max-retries` 调为正数。

### 安全和退出码

- `--inspect` 不读取 `.env`，不联网；
- `--publish` 需要 Langfuse 凭据，但不调用 Chat Provider；
- `--calibrate` 和 `--run` 同时要求 `--allow-real-provider`、
  `MULTIMODAL_AGENT_PROVIDER_MODE=real` 和完整真实 Chat 配置；
- `--run` 还要求 Langfuse 凭据与可用的 OTLP Trace 导出；
- Agent 不通过返回 1；
- 凭据、Environment、Dataset、Trace、Judge、Evidence 或 Score 故障返回 2；
- 通过返回 0。

### 评分与基础设施

固定 Score：

```text
agent_eval.reward
agent_eval.dimension.tool_execution
agent_eval.dimension.tool_use
agent_eval.dimension.state
agent_eval.dimension.response
```

`tool_execution` 判断 Validator、工具调用和结构化终态是否完整，不要求外部业务结果必须成功；
`tool_use` 判断 Agent 是否正确使用工具，包括选择、参数、次数、顺序、结果消费和恢复策略；
`state` 判断预期状态转换；
`response` 判断最终回答。四个必要维度全部通过时，`agent_eval.reward=1.0`。

Rule 与 LLM Judge 是判断机制，不是独立质量维度。它们分开实现并统一产出 assertion：

```text
tool_use
  outcome_matches_environment  [rule]
  arguments_correct            [rule]

response
  response_generated            [rule]
  outcome_evidence_usage       [judge]
```

Rule 结果具有确定性权威，Judge 不得覆盖 Rule。Judge Provider 超时、输出不可解析、criterion 缺失或
未返回 verdict 属于评测基础设施失败，不得记录为 Agent assertion 失败。Judge assertion 使用稳定
`criterion_id`；校准文件以 `judge_verdicts` 分别标注每个 criterion，不能用一个笼统语义标签代替
多个不同判断。每条 assertion 还必须提供面向评测查看者的短 `label`；内部 key 只用于稳定定位，
不能单独充当 comment。

维度 comment 在通过时展示“检查数量 + 全部 assertion label”，失败时展示“检查数量 + 失败
assertion 的 label + 真实 reason”；主要 reward comment 在通过时列出全部必要维度中文名，失败时
列出失败维度中文名及其具体失败原因。Langfuse Score metadata 把每条 assertion 的 `passed`、
`label`、`method` 和可选 `criterion_id` 写成独立标量字段，避免把完整 rubric、reason 或嵌套大对象
传播成超长属性。

工具业务结果预期以 Environment 的强类型声明为唯一事实源：

```python
ToolOutcomeExpectation.must_succeed("weather")

ToolOutcomeExpectation.must_fail_with(
    "weather",
    error_code="provider_timeout",
)

ToolOutcomeExpectation(
    tool_name="web_search",
    required=False,
    expected_result="success",
)
```

校准和 Langfuse Experiment 都通过通用 `grade_task()` 自动比较实际 `tool.finished/tool.failed` 与
错误码，并把结果写入 `tool_use` 的 `outcome_matches_environment` Rule assertion。预期成功但实际
超时会保持 `tool_execution=pass`，同时确定性地产生 `tool_use=fail`。Task grader 不再重复
硬编码工具应成功还是失败。评分时 Environment 按本次 Evidence 的实际 `available_tools` 生成完整
预期集合，因此真实配置与离线配置的正常工具数量不同也不会造成覆盖漂移。

`outcome_matches_environment` 只证明受控世界按声明运行，不证明 Agent 正确理解了结果。需要判断
最终回答是否把失败当成功、是否编造工具未提供的事实时，Task grader 应在 `response` 定义
`outcome_evidence_usage` Judge assertion。天气超时 Task 同时校准诚实恢复、超时后编造预报以及
重复调用三种 Evidence。

`tool_use` 是当前评分契约名；历史 Experiment 中已有的 `agent_eval.dimension.tool_semantics`
不会被回写，新运行只生成 `agent_eval.dimension.tool_use`。

Environment validation、凭据、Dataset、Trace 导出、Evidence 解析和 Judge 故障属于评测基础设施
失败，退出 2，不生成或篡改 Agent reward。Task-local 原子断言只解释某个维度为什么通过或失败。

### 从 Langfuse UI 触发 CLI

Assistant Server 提供默认关闭的 Remote Custom Experiment webhook：

```text
POST /internal/evals/langfuse/remote-experiment
```

它只负责验签、校验统一 Dataset 和 Git 中已有的 Task/Suite，然后在后台以固定 argv 启动
`scripts/run_agent_evals.py --run`。请求不能传入 shell、环境变量、env file、写权限或其他 CLI
参数；Task、Environment、Grader 和 reward 仍完全使用仓库中的定义。CLI stdout、stderr 和状态回执
写入 `.data/evals/remote/<trigger_id>.*`，不提交。

先在 Assistant Server 的本机未跟踪环境中配置：

```text
MULTIMODAL_AGENT_PROVIDER_MODE=real
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_ENABLED=true
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_SIGNING_SECRET=<Langfuse-setup-secret>
ASSISTANT_AGENT_LANGFUSE_REMOTE_EXPERIMENT_DATASET=assistant-agent-regression
```

然后在 Langfuse Dataset 页面选择 `Start Experiment -> Custom Experiment`：

1. URL 填
   `http://assistant-agent-eval-webhook/internal/evals/langfuse/remote-experiment`；
2. 开启 request signing，把生成的 signing secret 写入上述服务端环境；
3. 单 Task 的 default config 使用
   `{"task":"weather_timeout_recovery","runName":"ui-weather-timeout"}`；
4. Suite 使用 `{"suite":"release","runName":"ui-release"}`；
5. 先通过 `--publish` 确保所选 Task 已存在于统一 Dataset，再从 UI 点击 `Run`。

Langfuse `3.221.1` 的 webhook SSRF 校验只允许 URL 使用 80 或 443，host/IP whitelist 不会放行
8089。因此本机 Docker Compose 使用 `assistant-agent-eval-webhook` 在内部 80 端口提供单路径代理，
并设置 `LANGFUSE_WEBHOOK_WHITELISTED_HOST=assistant-agent-eval-webhook`；代理原样转发 body 和
`x-langfuse-signature` 到绑定 `0.0.0.0:8089` 的 Assistant Server。不要在 UI URL 中填写
`host.docker.internal:8089`。

webhook 校验 HMAC SHA-256 和五分钟时效，对相同签名与 body 的重投递只启动一次；收到请求后立即
返回 `202 Accepted`，CLI 继续异步执行。只有显式启用 webhook、配置签名 secret 且 Server 运行于
real Provider mode 时才接受触发。

真实运行生成的数据只保存在 Langfuse 和未跟踪 `.data/**`；不得提交凭据、原始生产 Trace、真实
用户数据或 Provider 原始响应。
