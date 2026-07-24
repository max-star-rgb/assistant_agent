# 个人助理 Eval 案例

`evals/` 保存 Agent 行为评测数据，不属于 pytest 默认测试范围。当前主案例集是
`personal_assistant_daily.json`，聚焦用户日常生活中的个人助理任务，验证模型能否理解目标、选择受治理
Tool、整合结果并给出可执行建议，而不是验证“用户说出 Tool 名后能否机械调用”。

## 案例分层

| 难度 | 数量 | 设计要求 |
| --- | ---: | --- |
| 简单 | 20 | 单一目标或单一信息源，但必须完成判断、整理、计算、行动建议或明确克制调用 |
| 中等 | 10 | 两类信息源或两步协作，包含顺序依赖、个性化上下文、媒体理解或确认边界 |
| 高级 | 5 | 三至四类 Tool 协同，要求规划、约束权衡、事实不确定性说明和最终行动编排 |

案例通过 `category=simple|medium|advanced` 标记难度。数量是语料契约；修改时应继续
保持 20/10/5，或在本文件和需求中明确升级版本。

## 当前 Tool 盘点

以下列表来自默认 mock Registry 的 `ToolSpec`。real 模式只会注册配置完整的真实实现，因此“案例期待
某 Tool”不代表运行环境已经具备该 Tool。

| Tool | 类别 | 日常用途 | 关键边界 |
| --- | --- | --- | --- |
| `weather` | read | 通勤、洗晒、遛狗、出行天气判断 | 需要地点；相对日期应标准化 |
| `calendar_search` | read | 查看安排、寻找空档、会前准备 | 只读，不替代创建事件 |
| `calendar_create` | write | 创建就医、聚会、旅行、搬家事件 | 必须显式确认 |
| `contacts_search` | read | 找家人、医生、老师、房东等联系人 | 只返回已配置联系人数据 |
| `shopping_search` | read | 按预算与需求筛选日用品或礼物 | 推荐需解释约束与取舍 |
| `web_search` | read | 查询近期活动、规则与公开事实 | 无专用 Tool 时使用 |
| `web_fetch` | read | 阅读指定公开页面并整理内容 | 只接受 HTTP(S) URL |
| `memory_search` | read | 搜索跨 session 的 daily memory records | 没有记录时不得编造 |
| `memory_get` | read | 按 ID 读取一条 daily memory record | 只能读取当前可信身份可见的记录 |
| `vision_understanding` | read | 理解冰箱、房间、服装等图片/视频 | request 必须携带结构化媒体 |
| `visual_image_search` | read | 从公开图片 URL 查找相似图片 | 不接受本地路径或 base64 |
| `image_generation` | generate | 生成邀请图、空间或穿搭预览 | 生成结果要标明仅供参考 |
| `python_interpreter` | dangerous | 精确计算家庭账单等本地分析 | 默认关闭，案例需结构化 opt-in |
| `tool_search` | read | 核心 Tool 不足时发现 MCP 候选 | 只发现，不执行也不授权 |

`task_plan_submit` 仅在 durable task 启用并绑定 service 时注册，本案例集不依赖 durable worker。

## 数据格式

案例遵循 `evals.real_provider.RealProviderEvalCase`：

- `expected_tools`：至少应出现的 Tool，不要求列表外 Tool 全部禁止；
- `expected_tool_sequence`：有真实数据依赖时使用，按有序子序列评分；
- `expected_exposed_tools`：预期进入本轮 `RunToolCatalog` 的 Tool；
- `must_not_call`：本任务明确不应调用的 Tool；
- `response_must_include` / `response_must_include_any`：只检查稳定、必要的回答事实；
- `min_tool_calls` / `max_tool_calls`：约束欠调用和无意义重复调用；
- `metadata.tool_confirmation`：为需要确认的写 Tool 提供显式确认；
- `metadata.tool_visibility`：为默认关闭的 Tool 提供结构化 opt-in。

媒体案例中的 `eval://images/...` 是固定 fixture 标识。执行真实媒体 eval 前，operator 必须在入口适配层
把这些标识映射到受控测试媒体；不能把真实用户照片提交到仓库。公开 URL 案例也应在正式运行前替换为
稳定、可访问且许可明确的测试资源。

## 运行方式

只校验格式、筛选 suite 和统计案例，不调用 Provider：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_real_provider_evals.py \
  --cases evals/personal_assistant_daily.json \
  --suite personal_assistant_daily \
  --dry-run
```

真实运行是显式 opt-in 操作。必须设置 `MULTIMODAL_AGENT_PROVIDER_MODE=real`、真实 chat provider，并
按所选 case 配置对应的 Tool Provider/MCP mapping。运行产物写入
`.data/evals/real_provider/<run>/`；不得提交 API key、Provider 原始响应或真实用户数据。

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=real \
MULTIMODAL_AGENT_CHAT_PROVIDER=deepseek \
DEEPSEEK_CHAT_API_KEY=... \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_real_provider_evals.py \
  --cases evals/personal_assistant_daily.json \
  --suite personal_assistant_daily
```

建议先按 category 或显式 `--case-id` 小批运行。当前 CLI 支持 suite、case id 和数量过滤，不直接支持
category 过滤；需要按难度批量运行时，可先从 JSON 生成临时 case 文件，或后续单独扩展 CLI。

## Langfuse 原生闭环评测

这里采用一条刻意简化的职责边界：

```text
Langfuse Dataset
  -> Langfuse Experiment
  -> evals/langfuse/experiment.py 执行 AgentGraphRuntime
  -> 结构化 AgentExperimentOutput
  -> Langfuse Code Evaluator
  -> Langfuse 原生 Score
```

Langfuse 是 Dataset、Evaluator、Score 和 Experiment 对比的运行时权威。项目不再注册 SDK
`evaluators` 或自行计算分数，只负责执行 Agent 并返回紧凑证据：

- Runtime 终态和最终回答；
- Tool 调用参数、结果和 ActionValidator 状态；
- 环境初始/最终状态和 state diff；
- Trace 事件名称、关联 ID 和总延迟。

`evals/langfuse/agent_closed_loop_v1.seed.json` 只是第一次创建 Dataset 或显式重置 seed
items 时使用的 bootstrap 文件。普通 Experiment 默认直接读取 Langfuse 中的 Dataset，不会用
本地 JSON 覆盖 UI 修改。

`evals/langfuse/agent_strict_pass.ts` 是 Langfuse Code Evaluator 的可版本控制部署源。
评分代码必须粘贴并启用在 Langfuse Evaluators 页面中；真正执行评分、保存 Score 和显示执行日志的
都是 Langfuse。首期只保留一个 `agent.strict_pass` Boolean Score，避免在入门阶段同时理解多套
评分聚合。

本 Dataset 的目标是验证 Agent 是否在受治理的 Runtime 中正确完成、克制或只读执行任务，并由
Tool Trace、Policy、环境状态变化和最终回答共同证明结果。scripted mock 只验证评测基础设施闭环，
不代表真实模型的泛化能力。

安装独立 optional dependency：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e '.[eval]'
```

定向验证：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/feature/eval
```

只校验可选 seed，不连接 Langfuse：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --dry-run
```

第一次显式创建 Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --seed-only
```

之后运行 Experiment，不再覆盖 Langfuse Dataset：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --run-name my-first-agent-eval
```

命令默认从未跟踪的 `.env` 加载 Langfuse 凭据和 host；可用 `--env-file` 覆盖或
`--no-env-file` 禁用。显式 Experiment 对 Dataset、Score 和 Runtime OTLP trace 采用 fail-fast；
这与普通生产观测的 fail-open 不同。当前命令固定使用 scripted mock，不调用真实 Provider。
普通 runtime 和默认 pytest 继续保持本地、mock、离线。

### 第一次亲手跑通

下面的流程不会调用真实模型或真实 Calendar，适合第一次体验完整闭环。

1. 进入项目目录，并确认 Python 环境：

   ```bash
   cd /home/lenovo1/pycharm_project/assistant_agent
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python --version
   ```

2. 确认 Langfuse 已启动：

   ```bash
   curl --noproxy '*' http://localhost:3000/api/public/health
   ```

   看到 `"status":"OK"` 即可继续。如果连接失败，先在 PyCharm 运行
   `.run/Langfuse.run.xml`。

3. 只检查案例文件，不连接 Langfuse：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
     scripts/run_langfuse_agent_evals.py --dry-run
   ```

   应看到三个 `item_ids`，分别覆盖写入、无需 Tool 和只读 Tool。

4. 运行 Runtime Task 契约测试：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
     tests/feature/eval
   ```

   这一步只验证 Agent 执行证据和 Langfuse Experiment wiring，不在项目进程里计算分数，
   也不会连接 Langfuse。

5. 第一次运行时，显式把 seed 导入 Langfuse：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
     scripts/run_langfuse_agent_evals.py --seed-only
   ```

   之后可直接在 Langfuse UI 编辑 Dataset；普通评测不会覆盖这些修改。

6. 在 Langfuse 的 `Evaluators` 页面创建 TypeScript Code Evaluator：

   - evaluator target 选择 `Experiments`；
   - Dataset 选择 `assistant-agent-closed-loop-v1`；
   - observation 选择 Experiment 的 `experiment-item-task`；
   - evaluator source 使用 `evals/langfuse/agent_strict_pass.ts`；
   - 先用 Preview/Test 验证，再启用 evaluator。

   本机自托管环境必须已经启用 Code Evaluator dispatcher，否则 UI 会禁用这项能力。
   当前可信本机 Compose 使用：

   ```text
   LANGFUSE_CODE_EVAL_DISPATCHER=insecure-local
   QUEUE_CONSUMER_CODE_EVAL_EXECUTION_QUEUE_IS_ENABLED=true
   ```

   两个变量必须同时提供给 Langfuse Web 和 Worker；`insecure-local` 会在 Worker
   进程内执行可信 TypeScript，不是处理不可信代码的沙箱。

7. 运行一次真正写入本地 Langfuse 的 Experiment。每次换一个 `run-name`，便于横向比较：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
     scripts/run_langfuse_agent_evals.py \
     --run-name my-first-agent-eval
   ```

   成功输出应包含 `item_count: 3`、`scoring: langfuse_code_evaluator_async`
   和 `dataset_run_url`。复制该 URL 到浏览器。Code Evaluator 是异步执行的，Score
   可能需要短暂等待后才出现。

8. 在 Langfuse 页面依次查看：

   - 三个 Dataset item 的 `agent.strict_pass`；
   - 点击 Score 查看 evaluator 来源和执行信息；
   - 点击一个 item 的 Trace；
   - 展开 `experiment-item-task -> assistant.runtime`；
   - 写入案例应出现 `calendar_create`，只读案例应出现 `calendar_search`，
     `no_tool` 案例不应出现 Tool observation。
   - 如需查评分代码本身的执行 Trace，在 Tracing 中筛选
     `environment = langfuse-code-eval`。

9. 想重复体验时，只需换一个名字再次执行第 7 步，例如：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
     scripts/run_langfuse_agent_evals.py \
     --run-name my-second-agent-eval
   ```

   两次 Dataset Run 会并存，不会主动删除旧 Trace，可在 Langfuse 中直接比较。
