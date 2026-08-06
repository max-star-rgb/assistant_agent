# Assistant 每日 Runtime 半自动审计设计

## 1. 目标

把当前需要人工选择时间窗口、分两次生成报告的 Runtime audit，调整为真正的半自动闭环：

1. 程序每天自动读取前一自然日的全部 Langfuse `assistant.turn`；
2. 本地 canonical JSONL 只用于完整性对账和缺失导出补证；
3. 程序自动调用隔离、只读的 Codex 审计整个 AgentRuntime；
4. Codex 自动生成通俗中文日报和修改建议；
5. 人只审核建议，系统不自动修改代码、Langfuse、Memory 或生产状态。

本设计不改变 Langfuse Live Observation evaluator、Agent Experiment 或 Score 的职责边界。

## 2. 已确认的产品决策

- 调度时区固定为 `Asia/Shanghai`。
- 每天 00:15 审计刚结束的前一自然日，即北京时间 00:00:00（含）到次日 00:00:00（不含）。
- 即使前一日没有 Trace，也生成一份极简中文日报，证明任务成功执行。
- `reports/` 只保存面向人的 Markdown；Codex 结构化 JSON 作为内部校验和状态证据，不再与日报并列。
- 日报正文使用普通中文解释“发生了什么、影响是什么、建议改什么、如何验证”；trace、observation、Score ID 只放证据附录。
- Codex 只提出建议，不生成补丁、不修改代码、不写 Langfuse、不写 Mem0。
- 当问题已被当天代码修改覆盖但没有复测 Trace 时，标记为“代码层面已处理，等待自然验证”，不谎称“已修复”，也不每天重复同一修改建议。

## 3. 用户体验

### 3.1 自动运行

正常使用不需要手工执行 `collect`、`report` 或变换 `--window-hours`。user timer 每天 00:15 调用一次完整流水线：

```text
确定待审计日期
  -> 读取该自然日 Langfuse Trace / Observation / Score
  -> 扫描同一自然日的本地完整性事实
  -> 生成内部 bundle
  -> 调用只读 Codex
  -> 校验结构化结果
  -> 更新内部问题状态
  -> 生成 reports/YYYY-MM-DD.md
```

若电脑关机，`Persistent=true` 使 timer 在下次启动时补触发。daily runner 根据内部 watermark 补齐尚未完成的自然日，而不是把启动时刻前 24 小时误当成一个自然日。首次启用且没有 watermark 时只审计前一日，不自动回扫全部历史。

### 3.2 手工重跑

手工入口仍用于排障或刷新某一天，但不再是日常必要操作：

```bash
python scripts/run_runtime_audit.py run
python scripts/run_runtime_audit.py run --date 2026-08-05
```

无参数 `run` 默认处理前一自然日；`--date` 精确重跑指定自然日。现有 rolling-window 能力保留为高级诊断参数，并与 `--date` 互斥，不作为教程主入口。

同一日期重跑时，人读日报仍只有一个 `reports/YYYY-MM-DD.md`，通过原子替换刷新；内部每次 attempt 使用唯一 ID 保留输入和结构化输出，以便诊断重跑差异。

## 4. Artifact 边界

```text
.data/runtime_audit/
  inbox/
    <attempt_id>.json                 # 内部只读 audit bundle
  state/
    watermark.json                    # 最近完成的自然日和 attempt
    latest-bundle.json                # 高级 collect/report 的最近 bundle 指针
    issues.json                       # 跨日报问题状态
    attempts/<attempt_id>.json        # Codex 结构化输出和执行状态
    schemas/<schema_version>.json     # Codex 输出 schema
  reports/
    YYYY-MM-DD.md                     # 唯一面向人的日报
```

Codex 仍须输出 JSON，因为：

- JSON Schema 可以拒绝随意 prose、缺字段和越权声明；
- 程序需要可靠地更新问题状态和渲染 Markdown；
- 自动化不能依赖解析自由格式文本。

但该 JSON 是内部机器 artifact，不是第二份人读报告。已有 `reports/*.json` 和旧命名 Markdown 作为历史产物保留，不做破坏性迁移；新运行不再向 `reports/` 写 JSON。

## 5. 自然日与时间边界

daily runner 先在 `Asia/Shanghai` 计算审计日期，再把边界转换为 UTC 查询 Langfuse 和本地事件：

```text
audit_date = 2026-08-05
local start = 2026-08-05 00:00:00 +08:00
local end   = 2026-08-06 00:00:00 +08:00
query start = 2026-08-04 16:00:00Z
query end   = 2026-08-05 16:00:00Z
```

区间固定为左闭右开，午夜事件不会同时进入两份日报。报告标题和文件名使用审计日期，不使用任务实际启动时间。

## 6. 问题状态与当场修复

### 6.1 不改写历史

坏 Trace 永远保留为“当时确实发生过”的证据。代码修改不能把旧 Trace 改成成功，也不能删除旧 finding。

### 6.2 状态模型

每个可持续跟踪的问题使用稳定 `issue_key`，按问题类别、组件、失败模式和规范化证据生成，不以单个 trace ID 作为问题身份。状态包括：

| 状态 | 含义 | 日报位置 |
| --- | --- | --- |
| `open` | 已观察到问题，未发现可信处理证据 | 需要你决定 |
| `code_addressed` | Trace 后存在相关代码/测试修改，但没有后续运行证据 | 已处理，等待自然验证 |
| `runtime_verified` | 后续自然 Trace 证明同类场景行为正常 | 昨日已验证解决 |
| `regressed` | 已处理或已验证的问题再次出现 | 需要你决定，标记再次出现 |
| `uncertain` | 证据不足，无法可靠关联代码或行为 | 观察项，不给确定修改结论 |

### 6.3 代码层面处理证据

Codex 在只读仓库中检查 Trace 时间之后、日报生成之前的 Git 和当前源码，证据强度依次为：

1. commit subject/body 或已有机器记录显式关联 finding 或 trace；
2. commit 修改了建议涉及的 owning module，并新增或更新相关测试；
3. 当前代码已不存在报告描述的错误路径，并存在针对该回归的测试；
4. 只有时间接近或提交主题相似。

只有前 1～3 类证据可以进入 `code_addressed`；仅时间接近不能压掉问题，只能进入 `uncertain`。代码测试通过只能证明代码层面处理，不能升级为 `runtime_verified`。

daily audit 本身不执行 pytest，也不把“存在测试文件”表述为“测试已经通过”。若修复流程留下了可读取的验证结果，可以作为补充证据；否则日报只说明观察到代码和回归测试发生变化。`issue_key` 在日报首次归并问题时生成，因此不要求早于日报发生的修复 commit 预先知道该 key。

### 6.4 自然验证

不要求用户为了审计专门复测。未来自然出现可比较的同类 Trace 时，daily runner 自动更新状态：

- 同类行为正常：`code_addressed -> runtime_verified`；
- 同类问题再次出现：`code_addressed|runtime_verified -> regressed`；
- 没有同类新 Trace：继续保持 `code_addressed`，但不在“需要你决定”重复输出同一修改建议。

`code_addressed` 在首次发现代码证据的日报中完整展示；后续日报只在“等待自然验证”中保留一行状态，直到自然验证或复现。

## 7. 人读日报

### 7.1 有 Trace 的日报

```markdown
# Assistant 每日审计报告：2026-08-05

## 一句话结论
昨天整体是否正常，最需要关注什么。

## 昨日概况
发生多少次对话、工具调用和记忆提取；不堆砌 observation 名称。

## 需要你决定
只列 open 和 regressed。每项说明：发生了什么、用户影响、建议改什么、如何验证。

## 已处理，等待自然验证
列出 code_addressed，明确“代码已改，但尚无真实运行证据”。

## 昨日已验证解决
只列当天由自然 Trace 获得验证的问题。

## 记忆情况
用普通语言说明提取、召回、脏记忆和证据限制。

## 系统运行情况
正常时一句话；只有导出、Score、Judge 或内容缺口异常时展开。

## 证据附录
集中保存 issue_key、trace、observation、Score、commit 和测试引用。
```

正文避免把 `manifest`、`fallback`、`observation`、`terminal_status` 等内部名词当作解释本身。必要时先翻译成人能理解的行为，再在附录保留机器引用。

### 7.2 无 Trace 的日报

```markdown
# Assistant 每日审计报告：2026-08-05

昨日无可审计对话。审计任务运行正常，无修改建议。

系统检查：Langfuse 可读，本地完整性来源可读。
```

只有 Langfuse Trace、本地 assistant turn 和 local fallback 同时为空时，才算真正的无 Trace 日期并跳过 Codex。若 Langfuse 没有 Trace、但本地存在缺失导出的 turn/fallback，仍须进入 Codex 审计观测缺口。若 Langfuse 不可读，则不能写“无可审计对话”，而应生成“审计未完成”的故障日报。

## 8. Codex 输出契约

结构化模型继续作为内部边界，但字段转向人读日报和问题生命周期：

- `daily_summary`：普通中文一句话结论；
- `activity_summary`：对话、工具、记忆的可读概况；
- `issues[]`：`issue_key`、状态、影响、通俗说明、建议、验证方式、证据引用；
- `memory_summary`：提取和召回结论；
- `infrastructure_summary`：观测完整性；
- `limitations[]`：不能确认的事实；
- `production_mutation_allowed=false`。

Prompt 明确要求：先回答“用户会感受到什么”和“维护者需要决定什么”，再给技术证据；不把基础设施缺口伪装成 Agent 质量失败；不因代码变化自动宣称真实修复。

## 9. 失败与降级

| 情况 | 行为 |
| --- | --- |
| Langfuse 可读且无 Trace | 生成正常空日报，不调用 Codex |
| Langfuse 不可读 | 生成“审计未完成”日报，不把本地 Trace 当成完整远端替代 |
| 本地 JSONL 不可读 | 继续审计 Langfuse，报告完整性对账不可用 |
| Judge Score pending | 标记评分仍在生成，不算质量失败 |
| Codex 调用失败或超时 | 保留 bundle，生成通俗的自动分析失败日报，issues 状态不前移 |
| Codex JSON schema 不合法 | 与 Codex 失败相同，不解析自由文本 |
| 单日重跑失败 | 保留上一次成功日报，并在内部 attempt 状态记录失败；不以失败内容覆盖成功日报 |
| 多日补跑中某日失败 | 记录该日失败，停止跨越 watermark；下次从该日重试 |

所有观测、报告和状态操作继续 fail-open，不改变 Assistant Runtime 的业务结果。

## 10. 调度

systemd user timer 调整为每天北京时间 00:15：

```text
OnCalendar=*-*-* 00:15:00 Asia/Shanghai
Persistent=true
RandomizedDelaySec=2min
```

service 仍强制 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`，使用显式 Codex executable，调用无参数 daily `run`。timer 只负责触发；自然日、补跑、幂等和日报命名由应用代码负责。

## 11. 安全边界

- Codex 使用 `--ephemeral --sandbox read-only`。
- Codex 子进程移除 Langfuse、Provider、Token 和其他 credentials。
- daily audit 不调用 Assistant Chat Provider、业务 Tool 或 Mem0。
- Langfuse Live evaluator 的 Judge 调用由 Langfuse 原生规则管理，daily audit 只读取已有 Score。
- 日报不得复制完整用户对话、Memory 正文或 Provider 原始响应；正文用最小必要摘要，机器 ID 放附录。
- `issues.json`、bundle、Codex JSON 和日报都是本地 `.data/**` artifact，不提交 Git。

## 12. 验证策略

本变更不修改现有 core invariant；使用 `tests/tdd/runtime_audit/` 做临时 RED/GREEN，覆盖：

1. `Asia/Shanghai` 前一自然日和 UTC 左闭右开边界；
2. 首次运行只处理昨日，watermark 存在时补齐缺失自然日；
3. 无 Trace 生成极简中文日报且不调用 Codex；
4. 有 Trace 自动调用 Codex，并只在 `reports/` 生成 Markdown；
5. Codex JSON、schema 和 attempt 状态进入内部目录；
6. 同日成功重跑原子替换日报，失败重跑不覆盖成功日报；
7. `open -> code_addressed -> runtime_verified|regressed` 状态转换；
8. 没有复测 Trace 时 `code_addressed` 不被误写为已修复；
9. 中文报告把机器 ID 集中到证据附录；
10. systemd unit/timer 通过 `systemd-analyze --user verify`。

定向验证保持 mock/local/offline，不调用真实 Provider、Langfuse Judge 或 Mem0。

## 13. 不在本次范围

- 自动修改代码、自动提交或自动回滚；
- 为每条 Trace 实时调用 Codex；
- 强制用户重放或复测请求；
- 删除历史 Langfuse Trace、旧报告或内部 artifact；
- 自动生成 Agent Experiment Task；
- 通知推送、Web UI 或日报趋势面板；
- 根据一次 Score 自动封禁 Tool、Memory 或 Provider。
