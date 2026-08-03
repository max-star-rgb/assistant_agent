# Observability 权威文档抽象实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将膨胀的 observability 当前权威重写为稳定架构契约，并新增一份可执行的真实运行诊断 runbook。

**Architecture:** `docs/observability-harness.md` 只定义稳定事实、语义和安全边界；新的
`docs/observability-diagnosis-runbook.md` 只定义如何用当前工具取得和解释机器证据。入口文档按
“理解架构”和“诊断真实运行”分别路由，具体字段和完整 CLI 参数留在源码及脚本帮助中。

**Tech Stack:** Markdown、Python/Pydantic observability models、JSONL、Langfuse、Git 文档检查工具。

## Global Constraints

- 只重构文档信息架构，不改变运行时、trace schema、日志行为或诊断工具。
- 当前源码和测试高于 prose；历史 Git 记录只用于识别内容来源。
- 验证保持 mock/local/offline，不调用真实 Provider。
- 不复制源码字段全集、脚本 `--help` 或 Langfuse UI 偶然表现。
- 不修改或回滚工作区内与本任务无关的现有改动。
- 新增设计和计划文档按仓库规则不自动提交；本任务所有改动均不提交，除非用户另行明确要求。

## 文件结构

- 重写：`docs/observability-harness.md`——稳定 observability 架构和契约的唯一权威。
- 新建：`docs/observability-diagnosis-runbook.md`——真实 run/trace/日志的现役诊断操作入口。
- 修改：`AGENTS.md`——分别路由架构阅读与真实运行诊断。
- 修改：`README.md`——并列提供 observability 架构与诊断入口。
- 修改：`scripts/README.md`——移除不存在的 `scripts/gateway_view.py`，说明当前可用的 runtime viewer
  与 Gateway JSONL 入口。
- 保留：`docs/superpowers/specs/2026-07-31-observability-authority-abstraction-design.md`——本次已批准设计记录。
- 保留：`docs/superpowers/plans/2026-07-31-observability-authority-abstraction.md`——本实施计划。

---

### Task 1: 用当前源码重建稳定 observability 权威

**Files:**

- Modify: `docs/observability-harness.md`
- Reference: `src/assistant_agent/observability/trace_store.py`
- Reference: `src/assistant_agent/observability/trace_persistence.py`
- Reference: `src/assistant_agent/observability/turn_summary.py`
- Reference: `src/assistant_agent/observability/agent_service_delivery.py`
- Reference: `src/assistant_agent/observability/agent_service_latency.py`
- Reference: `src/assistant_agent/observability/trace_content_policy.py`
- Reference: `src/assistant_agent/observability/otel_mapping.py`
- Reference: `src/assistant_agent/gateway/observability.py`
- Reference: `tests/core/contract/test_observability_contract.py`

**Interfaces:**

- Consumes: 当前 Pydantic model、事件构造函数、存储实现和 core observability contract。
- Produces: 一份按语义而非开发历史组织的稳定权威，供 runbook 和其他架构文档引用。

- [ ] **Step 1: 建立当前事实核对表**

逐项核对以下稳定事实及其源码所有者，只记录“语义 + 所有者”，不复制完整字段：

```text
canonical trace/event model     -> observability/trace_store.py
buffered/local persistence      -> observability/trace_persistence.py
assistant.turn.summary          -> observability/turn_summary.py
Agent-Service delivery audit    -> observability/agent_service_delivery.py
critical-path latency summary   -> observability/agent_service_latency.py
content capture/redaction       -> observability/trace_content_policy.py + trace_store.py
Langfuse/OTel projection        -> observability/otel_mapping.py
Gateway lifecycle JSONL         -> gateway/observability.py
stable core invariants          -> tests/core/contract/test_observability_contract.py
```

使用：

```bash
rg -n "schema_version|canonical_event|SAFE_|redact|fail|CompositeTraceStore" \
  src/assistant_agent/observability src/assistant_agent/gateway/observability.py \
  tests/core/contract/test_observability_contract.py
```

预期：每个将写入权威文档的稳定声明都能定位到当前源码或 core contract。

- [ ] **Step 2: 将 harness 重写为稳定章节**

将现有 1129 行内容重组为以下章节，合并重复叙述：

```markdown
# Observability Harness
## 定位与权威边界
## 观测面及职责
## 关联标识
## Canonical trace 与 span
## Turn、delivery 与 latency 摘要
## Operational logging 与持久化
## Content capture、redaction 与访问边界
## Langfuse、OTel 与评估投影
## 长期不变量
## 实现与验证入口
## 更新规则
```

每节只保留稳定语义、责任边界和必要的 schema/version 名；复杂字段明确指向其源码 model。

- [ ] **Step 3: 删除非权威内容**

确认新 harness 不再包含：

```text
Phase 1 ... Phase 5
Offline Improvement Lab 的开发阶段计划
agentruntime_view.py 参数全集
不存在的 scripts/gateway_view.py
Langfuse Formatted 面板折叠行为
按单次 bug 修复顺序描述的 retry/finalize 演进史
可从 Pydantic model 直接读取的字段全集
```

运行：

```bash
rg -n "Phase [1-5]|gateway_view\\.py|Formatted 面板|follow-live-updates|follow-all-sessions" \
  docs/observability-harness.md
```

预期：无匹配；若某个术语属于稳定契约而必须保留，应改写为与实现历史无关的抽象表述。

- [ ] **Step 4: 检查文档规模和独立可读性**

运行：

```bash
wc -l docs/observability-harness.md
rg -n '^#{1,3} ' docs/observability-harness.md
```

预期：行数显著少于 1129；章节完整覆盖事实权威、标识、trace/span、summary、logging、
redaction、projection、invariants 和入口，不依赖 runbook 才能理解架构。

### Task 2: 建立真实运行诊断 runbook

**Files:**

- Create: `docs/observability-diagnosis-runbook.md`
- Reference: `scripts/agentruntime_view.py`
- Reference: `scripts/run_server.py`
- Reference: `src/assistant_agent/observability/trace_query.py`
- Reference: `src/assistant_agent/observability/langfuse_config.py`
- Reference: `src/assistant_agent/observability/operational_logging.py`

**Interfaces:**

- Consumes: Task 1 定义的事实权威和当前可执行的 viewer/query/logging 入口。
- Produces: 从一个真实 `trace_id` 或机器日志开始、能够得出分层证据结论的操作流程。

- [ ] **Step 1: 核验当前诊断入口**

运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py --help
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --help
test ! -e scripts/gateway_view.py
```

预期：runtime viewer 和 server 参数可读取；`scripts/gateway_view.py` 不存在，因此 runbook 不引用它。

- [ ] **Step 2: 写入标准取证流程**

runbook 固定采用以下证据顺序：

```text
1. 识别 trace_id/run_id/turn_id/delivery_id，不把它们混用。
2. 用 agentruntime_view 查询 canonical trace；需要时显式连接 loopback server。
3. 已配置且可访问 Langfuse 时，用同一 trace_id 核对远端 trace。
4. 入口或发送问题再查 .data/gateway_events.jsonl 和 delivery audit。
5. 用源码解释已观察到的事件，不以源码推演替代缺失的机器事实。
6. 把结论标记为机器事实、源码解释或推测。
```

文档明确：仅有标准 `assistant.turn: <32 位十六进制 ID>` 时默认按 Langfuse/canonical
`trace_id` 处理；身份、环境或时间不匹配时停止归因并补证。

- [ ] **Step 3: 只保留覆盖主要场景的命令**

runbook 中使用当前已验证的命令：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/agentruntime_view.py <trace_id> --sections overview,decision,timeline --errors

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/agentruntime_view.py last --follow --follow-include-existing

rg -n '<run_id>|<trace_id>' .data/gateway_events.jsonl .data/graph_trace.jsonl
```

说明 `--include-conversation` 只允许对显式启用内容捕获的 loopback server 使用；不展示所有参数组合，
完整参数指向 `--help`。

- [ ] **Step 4: 写入分层诊断矩阵和降级路径**

覆盖以下层级与主要证据：

```text
Gateway ingress/admission   -> Gateway lifecycle JSONL
Assistant Runtime           -> canonical trace + assistant.turn.summary
Provider                    -> llm.chat span status/latency/usage metadata
Tool/Memory                 -> action/tool/memory observation and terminal event
Agent-Service delivery      -> turn latency + delivery audit/ACK
```

覆盖 trace 不存在、只有部分 trace、server 重启、JSONL 未持久化、Langfuse 不可用、timeout/cancel
尚未收敛等场景。所有降级路径都必须说明“可以确认什么”和“不能确认什么”。

- [ ] **Step 5: 检查 runbook 与 harness 无重复定义**

运行：

```bash
rg -n "schema_version|Allowed .*fields|字段如下|Phase [1-5]" \
  docs/observability-diagnosis-runbook.md
```

预期：runbook 可以引用 schema 名，但不重新定义字段全集或历史 phase。

### Task 3: 同步仓库入口并修复直接漂移

**Files:**

- Modify: `AGENTS.md:25`
- Modify: `AGENTS.md:102`
- Modify: `README.md:12`
- Modify: `scripts/README.md:33`

**Interfaces:**

- Consumes: Task 1 的架构权威和 Task 2 的现役 runbook。
- Produces: 清晰、无失效脚本引用的仓库导航。

- [ ] **Step 1: 调整 AGENTS 路由**

将任务路由拆成：

```text
trace/observability 架构、redaction、事件契约 -> docs/observability-harness.md
真实测试/通话/run/trace/机器日志诊断       -> docs/observability-diagnosis-runbook.md，
                                             必要时再读 harness
```

保留现有“先读取机器事实，再结合用户片段和源码回答”的约束。

- [ ] **Step 2: 调整 README 导航**

在现有 Observability 导航附近并列两个入口：

```markdown
- Observability architecture and trace contract: [docs/observability-harness.md](docs/observability-harness.md)
- Real-run diagnosis runbook: [docs/observability-diagnosis-runbook.md](docs/observability-diagnosis-runbook.md)
```

- [ ] **Step 3: 修正 scripts 索引**

删除 `scripts/gateway_view.py` 条目；保留 `scripts/agentruntime_view.py` 并说明 Gateway lifecycle
当前写入 `.data/gateway_events.jsonl`，可用标准文本/JSONL 工具按关联 ID 检索。

- [ ] **Step 4: 检查所有当前引用**

运行：

```bash
rg -n "observability-harness\\.md|observability-diagnosis-runbook\\.md|gateway_view\\.py" \
  AGENTS.md README.md scripts/README.md docs/*.md
```

预期：架构引用仍指向 harness；真实诊断入口指向 runbook；当前权威和入口不再引用不存在的
`gateway_view.py`。

### Task 4: 文档一致性与离线验收

**Files:**

- Verify: `docs/observability-harness.md`
- Verify: `docs/observability-diagnosis-runbook.md`
- Verify: `AGENTS.md`
- Verify: `README.md`
- Verify: `scripts/README.md`

**Interfaces:**

- Consumes: Tasks 1–3 的完整文档变更。
- Produces: 无断链、无格式错误、与当前工具一致的最终文档集。

- [ ] **Step 1: 运行文档证据收集器**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  .codex/skills/assistant-agent-documentation-sync/scripts/collect_documentation_evidence.py \
  --repo-root . > /tmp/assistant-agent-observability-doc-evidence.json
```

检查输出中与本任务五个当前文档相关的 missing path/link；区分真实漂移、示例占位符和历史材料。

- [ ] **Step 2: 验证脚本帮助与文档命令**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py --help
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py --help
```

预期：runbook 使用的选项均存在；不启动 server，不调用 Provider。

- [ ] **Step 3: 运行 Markdown 和 Git diff 检查**

```bash
git diff --check -- \
  AGENTS.md README.md scripts/README.md \
  docs/observability-harness.md docs/observability-diagnosis-runbook.md \
  docs/superpowers/specs/2026-07-31-observability-authority-abstraction-design.md \
  docs/superpowers/plans/2026-07-31-observability-authority-abstraction.md

git diff --stat -- \
  AGENTS.md README.md scripts/README.md \
  docs/observability-harness.md docs/observability-diagnosis-runbook.md
```

预期：无 whitespace error；harness 显著净减少，runbook 和路由变更范围符合设计。

- [ ] **Step 4: 检查未触碰无关改动**

```bash
git status --short
git diff --name-status
git diff --cached --name-status
```

预期：本任务只新增或修改文件结构中列出的路径；其他已有脏文件保持原状。

- [ ] **Step 5: 汇报验证边界**

最终报告使用：

```text
Core invariant: unchanged.
Tests: not added or run because this is a documentation-only information-architecture change.
Real Provider: not called.
```

同时报告文档证据收集器、两个 `--help` 和 `git diff --check` 的实际结果，以及
`scripts/gateway_view.py` 漂移的修复方式。
