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

案例遵循 `assistant_agent.eval.real_provider.RealProviderEvalCase`：

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

`evals/langfuse/calendar_closed_loop_v1.json` 是首个闭环评测 Dataset 源文件；文件名为首期
Calendar 历史名称，Dataset 已扩展为多能力集合。当前闭环包括：

- `AgentEvalEvidence` 汇总 Runtime 终态、完整 Trace、环境初始/最终状态和 state diff；
- stateful Calendar fixture 保存时间、地点、备注和幂等键，而不是只记录标题；
- `no_tool` 验证有候选 Tool 时仍能克制调用，并检查状态零污染；
- `read_only_tool` 验证查询参数、返回事件、回答依据和只读状态不变；
- 确定性 evaluator 返回 Langfuse 原生 `Evaluation`，形成 `agent.*` item scores；
- scripted mock chat adapter 实际驱动 `AgentGraphRuntime`，工具仍经过完整治理链；
- 正确创建与故意错误变体均在离线 feature tests 中验证，包括不必要调用、错误查询和写越界；
- Dataset Run evaluator 写入成功率、违规率、平均调用次数和延迟分位数；
- Langfuse Experiment task 把当前 W3C trace/span identity 传入 Runtime，Runtime observations、
  item scores 和 Dataset Run 属于同一条 Experiment trace。

安装独立 optional dependency：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pip install -e '.[eval]'
```

定向验证：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/feature/eval
```

只校验 Dataset 源文件，不连接 Langfuse：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py --dry-run
```

同步 Dataset 并运行本地 scripted Experiment：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_langfuse_agent_evals.py \
  --run-name calendar-closed-loop-local-v1
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

4. 运行离线 evaluator 测试：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
     tests/feature/eval
   ```

   看到 `23 passed` 表示 fixture、故意失败变体、Runtime 串联和 Langfuse adapter
   都通过；这一步仍然不会连接 Langfuse。

5. 运行一次真正写入本地 Langfuse 的 Experiment。每次换一个 `run-name`，便于横向比较：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
     scripts/run_langfuse_agent_evals.py \
     --run-name my-first-agent-eval
   ```

   成功输出应包含 `item_count: 3`、`strict_pass_rate: 1.0` 和
   `dataset_run_url`。复制该 URL 到浏览器。

6. 在 Langfuse 页面依次查看：

   - Dataset Run 顶部的 8 个聚合分数；
   - 三个 Dataset item 的 8 个 `agent.*` 分数；
   - 点击一个 item 进入 Trace；
   - 展开 `experiment-item-task -> assistant.runtime`；
   - 写入案例应出现 `calendar_create`，只读案例应出现 `calendar_search`，
     `no_tool` 案例不应出现 Tool observation。

7. 想重复体验时，只需换一个名字再次执行第 5 步，例如：

   ```bash
   /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
     scripts/run_langfuse_agent_evals.py \
     --run-name my-second-agent-eval
   ```

   两次 Dataset Run 会并存，不会主动删除旧 Trace，可在 Langfuse 中直接比较。
