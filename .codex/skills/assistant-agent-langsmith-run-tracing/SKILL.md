---
name: assistant-agent-langsmith-run-tracing
description: Use when an assistant_agent user provides an exact LangSmith run_id or trace_id and asks to locate, open, trace, inspect, or discuss that specific run.
---

# Assistant Agent LangSmith Run 追踪

精确 UUID 是主键。首次机器取证必须是 LangSmith exact retrieve；先命中记录，再按用户意图决定是否展开，不能让通用排查延迟首次定位。

## 意图与交付

| 用户意图 | 查询范围 | 首次交付 |
| --- | --- | --- |
| 定位、打开、确认、找到 | 只 retrieve 指定 ID | name、status、起止时间、耗时、直达 URL |
| 追踪、查看执行顺序、准备讨论 | retrieve 指定 ID，再按返回的 `trace_id` 一次加载 children | 上述字段，加紧凑的 root/node/LLM/Tool 时间线、调用次数、失败节点 |
| 为什么失败、异常、卡住、根因 | 先完成远端追踪，再进入诊断 | 定位、事实、最早失败边界、限制、下一步 |

“追踪”不是只返回 root；“定位”也不需要预先加载整棵 child tree。

## 快速路径

1. 校验 ID 是 UUID。使用当前已配置的 LangSmith client，按该 UUID 直接 retrieve；不要先列 project、做时间搜索或扫描用户正文。
2. 安全加载本机未跟踪配置，绝不输出 API key。若 shell 未继承配置而 8089 正在运行，按是否实际持有 LangSmith 配置识别 Python worker，不能按监听列表顺序猜父进程。
3. 命中后立即形成定位结果。用户只要求定位时到此停止。
4. 用户要求追踪时，只追加一次 trace children 查询，在同一进程中用返回对象完成时间线、计数和错误摘要的全部聚合。禁止为了补统计再次请求 LangSmith。保留节点名、run type、Tool 名、状态、时延、父子关系与 Feedback；不倾倒 prompt、message、Tool 参数、原始结果或全部 middleware spans。
5. 用户要求诊断时，才读取 `docs/observability-diagnosis-runbook.md`，并按需查询本地 canonical ledger、相关 authority 与源码。先区分 root terminal 和内部 child error，再做归因。

直接 retrieve 未命中、无权限或 LangSmith 不可达时，明确说明远端证据缺失，再进入 runbook 的降级路径；不得把未命中解释成 Runtime 未执行。

## 输出契约

追踪结果按以下顺序呈现：

1. 精确 ID、root/anchor name、status、北京时间、耗时、LangSmith URL。
2. 一张最小执行路线；并行分支明确标注并行。
3. LLM 与 Tool 数量、失败/重试/cancel/pending 摘要。
4. 证据来源和缺口。
5. 若用户要逐节点讨论，停在事实摘要，等待用户指定节点，不预先猜根因。

## 示例

用户：`01a03683-0184-7890-b836-813fb342c326 请追踪该 run_id，然后我们讨论`

正确行为：直接 retrieve，按 `trace_id` 一次加载 children，返回紧凑路线并等待讨论。既不能只报 root，也不能打印数百条原始 spans。

## 常见错误

| 错误 | 修正 |
| --- | --- |
| “多查一点更稳”，先读源码、日志和全部文档 | 精确 UUID 首次动作就是 exact retrieve |
| 选择第一个监听 8089 的 PID | 选择实际持有 LangSmith 配置的 worker |
| 原样输出 child tree，截断后再次查询 | 首次查询后直接在内存聚合 |
| 把“追踪”当成“定位”而提前停止 | 追踪必须提供紧凑 child 执行路线 |
| 看到内部 Tool error 就称 root 失败 | 分别报告 child status 与 root terminal |

## Red Flags

- 首次 LangSmith 请求前正在列 project、搜索时间范围、读取本地 ledger 或扫描源码。
- 已有完整 child 数据，却准备原样打印后再做第二次聚合查询。
- 用户只要求定位，却加载全部 children。
- 用户要求追踪，却只返回 root。
- 还没有机器事实，已经开始提出根因或修复方案。
