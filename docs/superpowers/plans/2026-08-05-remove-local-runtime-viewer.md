# 删除本地 Runtime 人类可读 Viewer 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除只供开发者在终端阅读完整 canonical timeline 的 `agentruntime_view.py`，将日常人工查看统一到 Langfuse，同时保留本地机器事实、结构化查询和完整性兜底。

**Architecture:** `TraceEvent -> CompositeTraceStore -> .data/graph_trace.jsonl` 与 `TraceEvent -> OTel -> Langfuse` 两条机器链路保持不变；只移除显式运行的终端 human viewer。诊断文档改为 Langfuse-first，Langfuse 缺失时使用现有结构化 HTTP 查询或 `rg` 检索 JSONL，不新增第二个 viewer。

**Tech Stack:** Python 3.12、FastAPI 结构化 trace API、OpenTelemetry/Langfuse、JSONL、pytest、Markdown。

## Global Constraints

- 不删除或改变 `TraceEvent`、`TraceStore`、`CompositeTraceStore`、`TraceQueryService`、`trace_debug_summary()`、`trace_event_summary()`。
- 不停止 `.data/graph_trace.jsonl` 持久化；Runtime Audit 继续把它用于完整性 manifest 与缺失导出 fallback。
- 不修改 OTel/Langfuse 投影、Score、Memory observation、Gateway concise console 或 WARNING/ERROR 日志。
- 不修改三个 `--allow-local-*` 内容开关。
- 不新增替代性的本地 human renderer、格式化 CLI 或重复 trace viewer。
- 不读取真实 Provider、不调用真实 Tool、不写入 Mem0 或 Langfuse；所有验证使用 mock/local/offline。
- `scripts/README.md` 当前含有其他任务改动，实施时只编辑 `agentruntime_view.py` 对应的一行，不覆盖或提交无关内容。

---

### Task 1: 删除独立 viewer，并把诊断入口迁移到 Langfuse-first

**Files:**
- Delete: `scripts/agentruntime_view.py`
- Modify: `scripts/README.md`
- Modify: `docs/observability-harness.md`
- Modify: `docs/observability-diagnosis-runbook.md`
- Modify: `docs/media-agent-service-websocket.md`
- Test: `tests/core/contract/test_observability_contract.py`（只运行，不修改）
- Test: `tests/tdd/runtime_audit/test_runtime_audit.py`（只运行，不修改）

**Interfaces:**
- Consumes: 现有 Langfuse `assistant.turn`、`GET /traces/{trace_id}`、`GET /traces/{trace_id}/conversation`、`.data/graph_trace.jsonl`、`scripts/run_runtime_audit.py`。
- Produces: 无本地人类 timeline CLI 的 Langfuse-first 诊断流程；保留原有结构化 API 和机器 trace 契约。

- [ ] **Step 1: 记录删除前基线与依赖边界**

运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/agentruntime_view.py --help

rg -n "agentruntime_view|trace_debug_summary|trace_event_summary|TraceQueryService" \
  scripts src docs tests \
  --glob '!docs/superpowers/**'
```

预期：viewer 帮助命令退出 0；引用审计证明 `trace_debug_summary`、`trace_event_summary` 和
`TraceQueryService` 还有非 viewer 消费者，后续不得删除。

- [ ] **Step 2: 删除独立 viewer 文件**

使用 `apply_patch` 删除：

```text
scripts/agentruntime_view.py
```

运行：

```bash
test ! -e scripts/agentruntime_view.py
```

预期：退出 0。

- [ ] **Step 3: 删除脚本索引中的 viewer 入口**

从 `scripts/README.md` 精确删除这一行，不改动相邻 Mem0、Runtime Audit 或其他任务内容：

```markdown
- `scripts/agentruntime_view.py`: canonical runtime trace viewer.
```

- [ ] **Step 4: 更新 observability 权威文档**

在 `docs/observability-harness.md` 的实现与验证入口表中删除 viewer 行：

```markdown
| `scripts/agentruntime_view.py` | canonical runtime trace 开发者 viewer |
```

保持 `trace_store.py`、`trace_query.py`、`otel_mapping.py` 与 Runtime Audit 的职责描述不变，并明确人工日常查看以 Langfuse 为主、本地 JSONL 只保留机器完整性和 fallback 职责。

- [ ] **Step 5: 把真实诊断 Runbook 改成 Langfuse-first + 结构化 fallback**

在 `docs/observability-diagnosis-runbook.md` 中删除以下 viewer 操作段：

```text
查看一个 canonical trace
跟随本地最新运行
agentruntime_view.py --help
conversation viewer 示例
```

替换为下列三层入口，保留原有证据优先级：

````markdown
### 查看 Langfuse trace

日常人工诊断先在本机 Langfuse 的 `assistant.turn` 中按精确 `trace_id` 查询，读取 observation、
input/output、status、usage 和 Score；不得因 UI 未命中就断言 Runtime 未执行。

### 查询仍在运行的 loopback Server

```bash
curl -fsS "http://127.0.0.1:8089/traces/<trace_id>" \
  | /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m json.tool
```

只有确需当前 turn 正文且 Server 使用 `--allow-local-trace-content` 启动时，才查询：

```bash
curl -fsS "http://127.0.0.1:8089/traces/<trace_id>/conversation" \
  | /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m json.tool
```

### Langfuse 或 Server 不可用时检查本地机器事实

```bash
rg -n --fixed-strings '<trace_id>' .data/graph_trace.jsonl
rg -n --fixed-strings '<run_id>' \
  .data/graph_trace.jsonl \
  .data/gateway_events.jsonl \
  .data/agent_service_delivery.jsonl
```
````

同时删除后文“使用 viewer”的措辞，保留 raw event 高于派生摘要、观测缺失不等于未发生、正文不得泄露等约束。

- [ ] **Step 6: 更新 Media-Agent 诊断示例**

在 `docs/media-agent-service-websocket.md` 中把两个 `agentruntime_view.py` 示例替换为：

```text
先在 Langfuse 中按 trace_id 查看 assistant.turn；若远端缺失，再按相同 trace_id 检索
.data/graph_trace.jsonl、gateway_events.jsonl 和 agent_service_delivery.jsonl。
```

不要在该协议文档中复制完整 Runtime Audit 教程或重建人类 timeline 格式。

- [ ] **Step 7: 验证 viewer 引用已经清零，但结构化消费者仍存在**

运行：

```bash
! rg -n "agentruntime_view" \
  scripts src docs tests \
  --glob '!docs/superpowers/**'

rg -n "trace_debug_summary|trace_event_summary|TraceQueryService" \
  src/assistant_agent \
  tests/core/contract/test_observability_contract.py
```

预期：第一条命令退出 0；第二条仍显示 runtime、API、multi-agent 和 core contract 消费者。

- [ ] **Step 8: 运行最小离线回归**

运行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/core/contract/test_observability_contract.py

MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_runtime_audit.py run --dry-run --skip-codex

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_server.py --help

git diff --check
```

预期：两组 pytest 全部通过；两个脚本帮助/dry-run 退出 0；`git diff --check` 无输出。不得调用真实 Provider、Judge、Tool 或 Memory。

- [ ] **Step 9: 审阅并提交原子变更**

先检查：

```bash
git diff -- scripts/agentruntime_view.py scripts/README.md \
  docs/observability-harness.md \
  docs/observability-diagnosis-runbook.md \
  docs/media-agent-service-websocket.md

git status --short
```

确认没有带入 `scripts/README.md` 的无关已有改动后，只提交本任务路径和本任务 hunk：

```bash
git commit --only -m "refactor(observability): remove local runtime viewer" -- \
  scripts/agentruntime_view.py \
  docs/observability-harness.md \
  docs/observability-diagnosis-runbook.md \
  docs/media-agent-service-websocket.md
```

若 `scripts/README.md` 仍混有其他任务的 working-tree hunk，不用 `git commit --only scripts/README.md` 提交整个文件；应只暂存 viewer 行删除的 hunk，或暂时保留该索引变更并在交付报告中说明。

最终交付必须报告：

```text
Core invariant: unchanged.
Tests: not added because this removes a developer-only human renderer; existing OBS-001 and runtime-audit tests were run unchanged.
```
