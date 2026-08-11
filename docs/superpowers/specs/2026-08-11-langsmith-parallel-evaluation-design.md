# LangSmith 与 Langfuse 并行评测设计

日期：2026-08-11

## 目标

在不替换、不削弱现有 Langfuse 观测与评测体系的前提下，引入一条独立可选的 LangSmith 链路，完整支持：

1. 日常生产 Runtime Trace 同时进入 Langfuse 和 LangSmith；
2. 在 LangSmith UI 中筛选或人工复核异常 Trace，并沉淀为固定 Dataset；
3. 通过代码驱动 LangSmith Dataset 在同一个生产 `AgentGraphRuntime` 上重跑；
4. 在 LangSmith Experiment 中查看结构化输入、参考输出、实际输出、完整 Trace 与绑定的评分；
5. 任一观测后端故障时，不破坏 Runtime 主流程或另一后端。

本设计不迁移现有 Langfuse Dataset、Experiment、Score、webhook 或 runtime audit，也不让两个平台相互成为事实源。

## 方案选择

采用“OTel 双写 + LangSmith 原生 Experiment Adapter”。

不使用 LangSmith `traceable()` 重写 Runtime 埋点树。Runtime 继续只产生一套 OpenTelemetry span，由独立 exporter 投影至不同后端。LangSmith 的离线实验使用其 Python SDK `Client.evaluate()`，target function 仅负责把 Dataset Example 编译为 `UserRequest` 并调用现有 Runtime。

该方案相对全面 SDK 包裹的侵入更小，也比只接入离线实验更完整：日常 Trace 可以直接在 LangSmith UI 中进入 Annotation Queue 或 Dataset，形成真实产品问题闭环。

## 架构边界

### 共享部分

- 唯一 Agent 行为实现仍是 `AgentGraphRuntime` / assistant loop。
- Tool 调用仍经过 `ActionValidator -> ToolExecutor -> ToolRegistry -> tool`。
- Provider 仍只分 `mock` 和 `real`，真实回归必须经过现有 operator 授权门槛。
- 输入、参考输出和实际输出使用项目统一 Runtime Regression JSON 契约。
- Runtime 只生成一棵 OTel span 树，不因后端数量复制 Agent loop。

### Langfuse 独立部分

- 现有 Langfuse exporter、字段映射、日常评分、runtime audit、Dataset、Experiment、Score 回查和 webhook 全部保留。
- Langfuse 仍按现有配置启用；LangSmith 的开关和故障不能改变其行为。
- 固定 Langfuse Dataset 继续为 `assistant-agent-runtime-regressions`，但其 Item 不自动同步到 LangSmith。

### LangSmith 独立部分

- 新增独立配置、OTel exporter、字段映射和 Runtime Regression runner。
- 固定 LangSmith Dataset 同样命名为 `assistant-agent-runtime-regressions`，仅在 LangSmith workspace 内生效。
- Dataset Example、Experiment、Feedback 和 Annotation Queue 均由 LangSmith 保存。
- LangSmith UI evaluator 绑定到 Dataset 后自动评价新 Experiment；项目代码不复制 Langfuse evaluator 配置。

## 配置契约

LangSmith 默认关闭。最小配置使用独立环境变量：

```text
ASSISTANT_AGENT_LANGSMITH_ENABLED=false
LANGSMITH_API_KEY=<untracked-secret>
LANGSMITH_ENDPOINT=https://api.smith.langchain.com
LANGSMITH_PROJECT=assistant-agent-runtime
LANGSMITH_WORKSPACE_ID=<optional-workspace-id>
```

规则如下：

- `ASSISTANT_AGENT_LANGSMITH_ENABLED=false` 时不创建 client 或 exporter，不要求凭据。
- 开关为 true 时必须具有 API key、合法 endpoint 和 project；配置不完整时明确报错，不静默降级。
- endpoint 可指向 LangSmith Cloud 或兼容的自托管实例；首期不交付自托管部署。
- `.env.example` 只记录空值和说明，不写真实 key。
- LangSmith SDK 作为 `eval` 可选依赖，不成为 Runtime 的强制依赖；没有安装 SDK 且功能关闭时项目仍可启动。

## 日常 Trace 双写

现有 OTel provider 保持唯一。观测装配根据两个后端的独立开关添加 span processor/exporter：

- Langfuse exporter 使用现有实现和 endpoint；
- LangSmith exporter 使用 OTLP/HTTP endpoint 与 LangSmith 认证 header；
- 两个 exporter 各自批处理、各自 flush、各自关闭；
- 单个 exporter 导出失败只进入诊断日志，不向 Runtime 请求路径抛出。

Span 同时携带两套后端映射字段。已有 `langfuse.*` 字段不改名、不删除；新增字段使用 LangSmith 官方 OTel 语义，例如：

- `langsmith.trace.name`
- `langsmith.span.kind`
- `langsmith.trace.session_id`
- `langsmith.span.tags`
- `langsmith.metadata.*`
- 根 span 的结构化 `inputs` / `outputs`

Runtime、LLM 和 Tool 的父子关系仍由同一 OTel context 决定。LangSmith 投影不得创建第二棵逻辑 Trace。

## LangSmith Dataset 契约

固定 Dataset：`assistant-agent-runtime-regressions`。

每个 Example 使用：

```json
{
  "inputs": {
    "role": "user",
    "content": "原始用户请求",
    "chars": 6,
    "truncated": false
  },
  "reference_outputs": {
    "role": "assistant",
    "content": "人工确认存在问题的原始回答",
    "chars": 15,
    "truncated": false,
    "terminal_status": "completed"
  },
  "metadata": {
    "source_trace_id": "...",
    "failure_category": "...",
    "captured_at": "...",
    "active": true
  }
}
```

约束：

- `inputs` 和 `reference_outputs` 必须是对象，不接受预序列化 JSON 字符串。
- `inputs.content` 必须是非空用户文本，`truncated=true` 的案例拒绝运行。
- `reference_outputs` 表示失败基线，不表示理想答案。
- 只运行 `metadata.active` 未显式设为 false 的 Example。
- LangSmith 的 Dataset version/split 可以用于 UI 管理，但 runner 不擅自修改 Example。

日常问题可在 LangSmith Trace 页面直接加入 Annotation Queue 或 Dataset。人工可在入库前修正 input、reference output 和 metadata。自动规则可以把低分 Trace 放入 Annotation Queue，但不直接赋予真实 Provider 重跑权限。

## LangSmith Runtime Regression

新增稳定 CLI 入口，与 Langfuse runner 并列命名。CLI 支持离线 inspect 和显式真实 run：

```text
python scripts/run_langsmith_runtime_regressions.py --inspect
python scripts/run_langsmith_runtime_regressions.py --run \
  --allow-real-provider --allow-runtime-side-effects
```

`--inspect` 只连接 LangSmith 读取并校验 Dataset，不调用主模型。`--run` 必须同时满足：

- `MULTIMODAL_AGENT_PROVIDER_MODE=real`；
- LangSmith 功能和凭据完整；
- `--allow-real-provider`；
- `--allow-runtime-side-effects`；
- Dataset 至少存在一个 active Example。

Runner 调用 `Client.evaluate()`：

1. SDK 从固定 Dataset 取出 Example；
2. target function 将 `inputs.content` 编译为 `UserRequest`；
3. target function 构造生产 Runtime 并执行 `run_state()`；
4. Runtime 产生的 OTel 子树通过 LangSmith exporter 关联到当前 Experiment Example；
5. target function 返回统一 assistant output；
6. LangSmith 保存实际输出，并触发 Dataset 上绑定的 UI evaluator；
7. runner flush 后回查 Experiment：每个 active Example 必须有一个结果、非空输入、结构化输出和 Runtime 根 Trace。

Experiment metadata 至少记录主模型、Git commit、evaluation mode 和 Dataset version。首次实现不自动比较 Langfuse 与 LangSmith 的评分，因为两边 evaluator 的版本和执行环境并非同一事实。

## 错误处理

### Runtime 请求路径

- LangSmith 未启用：零额外网络调用。
- LangSmith export 失败：记录 backend、endpoint、错误类型和可关联 trace id；Runtime 继续，Langfuse exporter 不受影响。
- Langfuse export 失败：保持现有行为；LangSmith exporter 不受影响。
- flush/close 分别执行，某个后端异常不能阻止另一个后端清理。

### Experiment 路径

- 配置、Dataset、Example、SDK、Provider 或 Trace 关联失败均视为 infrastructure failure。
- 不以 mock fallback 冒充真实实验，不把基础设施失败写成质量分数。
- 某个 Example 执行失败应保留该 Experiment 证据，最终命令返回非零。
- 完整性回查失败时报告缺失的 Example、Run 或 Feedback，不宣称实验成功。

## 安全与数据治理

- 两个平台均只接收当前观测策略允许导出的内容；敏感字段继续使用现有清洗边界。
- 不提交 key、真实用户数据、Provider 原始响应或 Experiment 产物。
- LangSmith 开关不会自动开启 real Provider。
- UI 自动化只能负责筛选、评分和沉淀，不能扩大工具副作用或 Provider 权限。
- 两套 Dataset 各自独立，避免双向同步产生循环、覆盖人工编辑或错误地合并评分语义。

## 测试与验证

Core invariant 不变。本功能的确定性 RED/GREEN 测试放在独立的 `tests/tdd/langsmith-parallel-evaluation/`，可由用户整目录删除，不自动晋升 core。

最小验证包括：

1. LangSmith 关闭或 SDK 缺失时，现有 Langfuse 装配结果不变；
2. 两个 exporter 同时启用时均收到同一 trace/span identity 与完整父子树；
3. 任一 exporter 抛错时另一 exporter 仍能 export、flush 和 close；
4. LangSmith span 映射保留结构化 input/output、session、LLM 与 Tool 类型；
5. Dataset validator 接受规范对象并拒绝空 input、字符串化 reference output 和 truncated input；
6. inspect 不调用 Runtime 或真实 Provider；
7. run 的授权门槛 fail closed；
8. target function 调用生产 Runtime，并返回统一 assistant JSON；
9. Experiment 完整性检查能发现缺失 Example 结果、输入、Runtime Trace 或绑定评分；
10. 现有 Langfuse Runtime Regression TDD 继续通过。

真实 LangSmith 验证必须由 operator 提供未跟踪凭据并显式授权。首个真实验收只运行一个人工 Dataset Example，核对：

- 日常 Trace 同时出现在两个平台；
- LangSmith UI 添加 Dataset 后 input/reference output 保持对象展示；
- Experiment 具有实际输出、完整 Runtime/LLM/Tool 子树和 UI evaluator Feedback；
- Langfuse 原有 Trace 与 Runtime Regression 仍可独立运行。

## 完成标准

以下事实全部成立才算完成：

- LangSmith 默认关闭时，项目与 Langfuse 行为无回归；
- 启用 LangSmith 后，日常 Trace 可在 LangSmith UI 中查看并沉淀；
- LangSmith 固定 Dataset 可通过新 runner 调用生产 Runtime 创建 Experiment；
- Experiment 的输入、参考输出、实际输出和 Trace 结构完整；
- 双后端故障隔离经过确定性测试；
- 权威文档、示例配置、依赖和脚本导航已同步；
- 真实 Provider 未在无显式授权时被调用。
