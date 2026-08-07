# 运行审计对话式日报实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将日常运行审计 Markdown 改成面向维护者的自然中文回复，并保证整份人读报告不出现任何机器证据 ID 或内部状态名。

**Architecture:** 保留 `DailyCodexAuditReport`、issue registry 和全部证据校验；Codex 仍输出结构化 JSON。只调整日审计 prompt 的写作要求和 `report.py` 的确定性人读投影，使机器证据继续存在于 JSON、但永不进入 Markdown。

**Tech Stack:** Python 3.12、Pydantic、pytest、Codex structured output、Markdown

## Global Constraints

- 人读 Markdown 不出现 Trace、observation、Score、Git SHA、测试路径、issue key 或内部状态名。
- 不生成证据附录，不放宽内部 JSON 的证据引用与生命周期校验。
- 有代码修改仍只表示等待真实运行验证，不能声称已经恢复。
- 正常有异常日报约 500～1000 个中文字；无异常日报保持一句直接结论。
- 不改变 Langfuse、本地 event、Git 采集、定时器、日期命名或幂等策略。

---

### Task 1: 对话式 Markdown 与机器 ID 隔离

**Files:**
- Modify: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`
- Modify: `tests/tdd/runtime_audit/test_final_review_fix.py`
- Modify: `src/assistant_agent/observability/runtime_audit/report.py`

**Interfaces:**
- Consumes: `DailyCodexAuditReport` 和确定性合并后的 `list[DailyAuditIssue]`
- Produces: `render_daily_codex_report(...) -> str`、`render_empty_daily_report(...) -> str`、`render_no_anomaly_daily_report(...) -> str` 的对话式 Markdown

- [x] **Step 1: 写失败测试**

  更新真实 renderer 行为断言：输出以直接结论开场；按“现在处理”“已经改了先观察”“暂时不能下结论”组织自然段；不得包含旧模板标签、证据附录、issue key、机器引用、裸 Git SHA、测试路径和内部状态名。

- [x] **Step 2: 运行测试确认 RED**

  Run:

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
    tests/tdd/runtime_audit/test_daily_runtime_audit.py \
    tests/tdd/runtime_audit/test_final_review_fix.py
  ```

  Expected: FAIL，旧 renderer 仍输出“发生了什么”“证据附录”或机器 ID。

- [x] **Step 3: 实现最小对话式 renderer**

  在 `report.py` 中按状态选择非空章节，用自然段组合 `plain_summary`、`user_impact`、`suggested_change` 和 `validation`；删除证据附录投影。扩展 `_plain_text`，清除意外混入自然语言字段的机器引用、UUID、裸 Git SHA、测试路径和内部术语。

- [x] **Step 4: 运行测试确认 GREEN**

  重复 Step 2 命令，Expected: PASS。

### Task 2: Codex 对话式写作约束与权威文档同步

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/runner.py`
- Modify: `docs/observability-harness.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: 第三层异常 JSON 与 issue registry JSON
- Produces: 既有 `DailyCodexAuditReport` JSON Schema；自然中文字段仍由 Python renderer 投影

- [x] **Step 1: 调整 prompt**

  要求 Codex 像直接回复维护者一样结论先行，禁止在人类正文字段写机器 ID、内部状态名和审计公文式转述；机器引用仍只写入结构化 evidence ref 字段。

- [x] **Step 2: 同步权威说明**

  更新运行审计文档：`reports/YYYY-MM-DD.md` 是无技术 ID 的对话式人读投影；第三层 JSON 与 registry 继续保留证据。

- [x] **Step 3: 运行静态检查**

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
    src/assistant_agent/observability/runtime_audit/report.py \
    src/assistant_agent/observability/runtime_audit/runner.py \
    tests/tdd/runtime_audit/test_daily_runtime_audit.py \
    tests/tdd/runtime_audit/test_final_review_fix.py
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
    src/assistant_agent/observability/runtime_audit
  git diff --check
  ```

  Expected: 全部成功。

### Task 3: 全量专项回归、提交与真实日报重跑

**Files:**
- Verify: `tests/tdd/runtime_audit/`
- Generate: `.data/runtime_audit/reports/2026-08-06.md`（本地生成物，不提交）

**Interfaces:**
- Consumes: 已发布的第二层 bundle、第三层异常索引、Langfuse 只读事实和本地 Git 证据
- Produces: 新版 `2026-08-06.md`

- [x] **Step 1: 运行完整专项测试**

  ```bash
  MULTIMODAL_AGENT_PROVIDER_MODE=mock /home/lenovo1/miniconda3/envs/hello_agent/bin/python \
    -m pytest -q tests/tdd/runtime_audit
  ```

  Expected: 全部通过。

- [ ] **Step 2: 提交任务代码**

  只提交本任务相关源码、测试和文档，提交信息使用：

  ```text
  feat(observability): make daily audits conversational
  ```

- [ ] **Step 3: 强制重跑昨天日报**

  ```bash
  /home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_runtime_audit.py \
    run --date 2026-08-06 --force --codex-timeout-seconds 900
  ```

  Expected: 成功生成 `.data/runtime_audit/reports/2026-08-06.md`。

- [ ] **Step 4: 检查实际报告**

  人工确认报告自然、结论先行，不包含证据附录、机器 ID、测试路径、Git SHA 或内部状态名；普通同日命令仍幂等跳过。
