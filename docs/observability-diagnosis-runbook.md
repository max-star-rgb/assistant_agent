# LangSmith run / thread 诊断 Runbook

最后更新：2026-08-31

## Authority contract

| 字段 | 内容 |
| --- | --- |
| 定位 | 给定 LangSmith `run_id` / `trace_id` / `thread_id` 后的机器事实快速定位与诊断权威 |
| Owns | ID 判别、LangSmith SDK 查询顺序、thread 聚合、证据降级、归因格式与敏感信息处理 |
| Does not own | trace schema、Graph/视觉行为、Agent Server/media wire contract、评测准入 |
| 源码与 schema 入口 | LangSmith Python SDK；当前 schema 见 `observability-harness.md` |
| 验证入口 | `docs/authority.toml` 中 `observability-diagnosis.verification` |
| 相邻 authority | [`observability-harness.md`](observability-harness.md)、[`agent-server-architecture.md`](agent-server-architecture.md)、[`visual-perception-architecture.md`](visual-perception-architecture.md) |

本文件是查询操作入口；字段、父子关系和脱敏规则只在 `observability-harness.md` 定义。后续用户只需提供
`run_id` 或 `thread_id`，Codex 应先执行这里的窄查询，不先扫描仓库、日志、project 列表或时间范围。

## 1. 先判别 ID

- 用户明确说 `run_id` 或 `trace_id`：按 LangSmith run UUID 直接读取。LangSmith 中 root、node、LLM、Tool
  都有各自 `run_id`；传入 child run 也先读该 child，再使用返回的 `trace_id` 展开整棵 trace。
- 用户明确说 `thread_id`：按 root metadata 的 `thread_id` 精确过滤。不要先把它尝试成 `run_id`。
- 只有用户只给一个 UUID、未说明类型时，才先 `read_run`；明确返回 404 后再把同一 UUID 当 `thread_id`
  查询。鉴权、网络或 project 错误不是 404，不得误判 ID 类型。

所有查询使用当前安全环境中的 `LANGSMITH_API_KEY`、`LANGSMITH_ENDPOINT` 与 `LANGSMITH_PROJECT`；不得打印
配置值。查询只读，不能为了诊断启用真实 Provider、补跑实验或修改 Feedback。

## 2. `run_id` 快速路径

在仓库根目录用当前 Python 进程调用原生 SDK：

```python
import os
from uuid import UUID
from langsmith import Client

run_id = UUID("<run_id>")
client = Client()
project = os.environ.get("LANGSMITH_PROJECT", "default")
run = client.read_run(run_id)
print({
    "run_id": str(run.id),
    "trace_id": str(run.trace_id),
    "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
    "name": run.name,
    "run_type": run.run_type,
    "status": "error" if run.error else ("completed" if run.end_time else "running"),
    "start_time": run.start_time,
    "end_time": run.end_time,
    "url": client.get_run_url(run=run),
})
```

用户只要求“定位/打开/确认”时，到这里结束。用户要求“追踪/执行顺序/展开”时，再执行一次有界 trace 查询：

```python
runs = sorted(
    client.list_runs(
        project_name=project,
        trace_id=run.trace_id,
        limit=1000,
    ),
    key=lambda item: item.start_time,
)
```

在同一进程、同一批返回对象上汇总关键 root/node/LLM/Tool 的父子关系、开始时间、terminal、latency、token、
error 与 Feedback。不要为了补统计重复请求；不要原样输出全部 middleware spans。root 成功而 child 曾失败时，
必须分别陈述，不能把整个 trace 自动判成失败。

## 3. `thread_id` 快速路径

AssistantAgent trace 与后台视觉 trace 不共享 `run_id`，但共享 `metadata.thread_id`。因此 thread 查询应只取
root runs；结果中通常可同时看到：

- AssistantAgent roots：原生 graph/node/LLM/Tool tree；
- `vision.observation` roots：每个已关闭关键帧窗口一个独立 trace，tag 为 `vision-observation`；
- 每个视觉 root 下的 `vlm.infer` child generation，以及 root 的有序 JPEG/MP4 attachments。
- `visual_reminder.*` roots：content-free 的 created/matched/delivery/cleared 生命周期，tag 为 `visual-reminder`。

精确查询当前 project，默认上限 100：

```python
import os
from uuid import UUID
from langsmith import Client

thread_id = str(UUID("<thread_id>"))
client = Client()
project = os.environ.get("LANGSMITH_PROJECT", "default")
metadata_filter = f'has(metadata, \'{{"thread_id":"{thread_id}"}}\')'
roots = sorted(
    client.list_runs(
        project_name=project,
        is_root=True,
        filter=metadata_filter,
        limit=100,
    ),
    key=lambda item: item.start_time,
)
```

先输出每个 root 的 `name / run_id / trace_id / status / start/end / URL`，并按 `name` 区分 AssistantAgent 与
`vision.observation`。用户要求展开某个窗口或对话时，再以该 root 的 `trace_id` 使用第 2 节查询 children。
若正好返回 100 条，说明结果可能截断；再依据最早命中时间做有界时间分页，不得改成无界 project 扫描。

token 统计只能选择一个口径：汇总 root 的聚合 token，或汇总 LLM child token；不得两层相加。视觉窗口 MP4
是窗口关闭时形成的短视频附件，不是持续更新的直播流；运行中的 trace/附件可能尚未完成上传，应报告
`running` 并在用户要求监控时轮询同一 run，不得新建或重跑。

## 4. 失败与证据降级

查询顺序固定如下：

1. LangSmith 精确 run/thread 查询；
2. 若 8089 dev worker 持有凭据但当前 shell 没有，识别该唯一 worker 的安全配置来源后在同等环境重试，
   不输出配置值，也不按监听列表顺序猜父进程；
3. 仅在 LangSmith 404、不可达或无权限时，按用户给定 ID 与时间范围查 Agent Server/媒体 transport 日志；
4. 历史 custom canonical JSONL 只用于诊断迁移前旧 run，不能作为当前 AssistantAgent 或视觉 trace 的主证据。

远端未命中不能推出 Runtime 未执行。明确区分 `not_found`、`unauthorized`、`network_unavailable`、
`wrong_project` 与 `evidence_not_retained`。不得输出 prompt/message content、Provider payload、Tool 原始参数或结果、
媒体内容、Authorization、API key；必要事实先脱敏并最小化。

## 5. 诊断输出格式

每次诊断至少给出：

- 定位：输入 ID 的类型、project、root/child 身份、URL 与证据来源；
- 时间线：关键 root/node/LLM/Tool 或 `vision.observation -> vlm.infer` 的父子/并行关系与 terminal；
- 归因：最早可证明的失败边界，区分 Graph、Provider、Tool、Agent Server/custom route、视觉后台与 exporter；
- 限制：缺失事实及不能据此得出的结论；
- 下一步：最小复现、继续轮询的同一 run，或应人工沉淀的 regression case。

维护规则：trace schema 或脱敏规则变化时更新 `observability-harness.md`；SDK 查询签名、filter 语法、project
选择或证据顺序变化时更新本文件，两者必须互相链接且不得复制两套查询命令。
