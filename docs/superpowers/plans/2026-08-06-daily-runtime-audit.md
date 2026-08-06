# Assistant 每日 Runtime 半自动审计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每天北京时间 00:15 自动审计前一自然日的全部 Runtime Trace，只向人暴露一份通俗中文 Markdown 日报，并持续区分待处理、代码已处理待自然验证、真实验证和复现问题。

**Architecture:** 保留 Langfuse-first collector，把自然日计算、daily orchestration、问题状态和人读 renderer 拆成独立模块。每次 attempt 保存不可变 bundle 与 Codex 结构化结果，按审计日期原子发布唯一 Markdown；systemd 只触发无参数 `run`，日期补跑和幂等由应用代码负责。

**Tech Stack:** Python 3.12、Pydantic、argparse、Langfuse SDK、Codex CLI structured output、systemd user timer、pytest。

## Global Constraints

- 默认 Python 固定为 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python`。
- 时区固定为 `Asia/Shanghai`，自然日查询区间为左闭右开。
- `run` 无参数默认审计前一自然日；`--date YYYY-MM-DD` 精确重跑；rolling window 只保留为显式高级诊断。
- 每天 00:15 自动运行，`Persistent=true`；首次运行只处理昨日，后续按 watermark 补齐缺失自然日。
- `reports/` 新产物只能是 `YYYY-MM-DD.md`；bundle、Codex JSON、schema、attempt 和 issues 状态全部位于内部目录。
- 无 Trace 时生成成功简报且不调用 Codex；Langfuse 不可读时生成“审计未完成”，不能伪装成空日。
- Codex 始终 `--ephemeral --sandbox read-only`，移除 credentials，`production_mutation_allowed=false`。
- daily audit 不调用 Assistant Provider、业务 Tool、Mem0 或 Langfuse Judge，只读取已有 Trace 和 Score。
- 历史坏 Trace 不改写；没有后续真实 Trace 时只能标记 `code_addressed`，不能标记 `runtime_verified`。
- 不删除或迁移已有 `.data/runtime_audit/reports/*.json` 和旧命名 Markdown。
- 测试仅放 `tests/tdd/runtime_audit/`，保持 mock/local/offline，不修改 `tests/core`。

---

### Task 1: 自然日窗口与 CLI 日期选择

**Files:**
- Create: `src/assistant_agent/observability/runtime_audit/daily_window.py`
- Modify: `src/assistant_agent/observability/runtime_audit/cli.py`
- Create: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`

**Interfaces:**
- Produces: `DailyAuditWindow`, `window_for_date(audit_date: date) -> DailyAuditWindow`、`previous_day_window(now: datetime) -> DailyAuditWindow`、`pending_audit_dates(*, yesterday: date, last_completed: date | None) -> list[date]`。
- Consumes: 现有 `collect_runtime_audit` 的 `window_start: datetime` 与 `window_end: datetime` UTC 边界。

- [ ] **Step 1: 写自然日边界和补跑选择的 RED 测试**

```python
from datetime import date, datetime, timezone

from assistant_agent.observability.runtime_audit.daily_window import (
    pending_audit_dates,
    previous_day_window,
    window_for_date,
)


def test_previous_day_uses_shanghai_calendar_boundaries() -> None:
    window = previous_day_window(datetime(2026, 8, 6, 1, 0, tzinfo=timezone.utc))
    assert window.audit_date == date(2026, 8, 5)
    assert window.start_utc == datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
    assert window.end_utc == datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


def test_pending_days_backfill_without_historical_first_run() -> None:
    assert pending_audit_dates(yesterday=date(2026, 8, 5), last_completed=None) == [date(2026, 8, 5)]
    assert pending_audit_dates(
        yesterday=date(2026, 8, 5),
        last_completed=date(2026, 8, 2),
    ) == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
```

- [ ] **Step 2: 运行 RED 并确认失败原因是模块尚不存在**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py \
  -k 'previous_day or pending_days'
```

Expected: collection error 包含 `ModuleNotFoundError` 和 `daily_window`。

- [ ] **Step 3: 实现最小自然日模型和函数**

```python
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel

AUDIT_TIMEZONE = ZoneInfo("Asia/Shanghai")


class DailyAuditWindow(BaseModel):
    audit_date: date
    start_utc: datetime
    end_utc: datetime


def window_for_date(audit_date: date) -> DailyAuditWindow:
    local_start = datetime.combine(audit_date, time.min, tzinfo=AUDIT_TIMEZONE)
    local_end = local_start + timedelta(days=1)
    return DailyAuditWindow(
        audit_date=audit_date,
        start_utc=local_start.astimezone(timezone.utc),
        end_utc=local_end.astimezone(timezone.utc),
    )


def previous_day_window(now: datetime) -> DailyAuditWindow:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    audit_date = now.astimezone(AUDIT_TIMEZONE).date() - timedelta(days=1)
    return window_for_date(audit_date)


def pending_audit_dates(*, yesterday: date, last_completed: date | None) -> list[date]:
    start = yesterday if last_completed is None else last_completed + timedelta(days=1)
    if start > yesterday:
        return []
    return [start + timedelta(days=offset) for offset in range((yesterday - start).days + 1)]
```

- [ ] **Step 4: 扩展 CLI parser 测试，约束 `--date` 与 `--window-hours` 互斥**

在同一测试文件通过 `_parser().parse_args(["run"])` 和冲突参数组合断言：

```python
def test_run_defaults_to_previous_calendar_day_and_date_conflicts_with_window_hours() -> None:
    parser = _parser()
    args = parser.parse_args(["run"])
    assert args.date is None
    assert args.window_hours is None
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--date", "2026-08-05", "--window-hours", "2"])
```

在 `cli.py` 为 `collect/run` 使用 mutually exclusive group：

```python
window = child.add_mutually_exclusive_group()
window.add_argument("--date", type=date.fromisoformat)
window.add_argument("--window-hours", type=float)
```

无参数时选择 `previous_day_window(datetime.now(timezone.utc))`；显式 `--window-hours` 才走现有 rolling window。

- [ ] **Step 5: 运行 Task 1 测试并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py

git add src/assistant_agent/observability/runtime_audit/daily_window.py \
  src/assistant_agent/observability/runtime_audit/cli.py \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py
git commit -m "feat(observability): add natural-day audit windows"
```

Expected: Task 1 tests pass；不运行网络或真实 Provider。

---

### Task 2: Daily artifact、attempt 与安全发布

**Files:**
- Create: `src/assistant_agent/observability/runtime_audit/daily_models.py`
- Modify: `src/assistant_agent/observability/runtime_audit/cli.py`
- Modify: `src/assistant_agent/observability/runtime_audit/storage.py`
- Modify: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`

**Interfaces:**
- Consumes: Task 1 `DailyAuditWindow.audit_date`。
- Produces: `DailyAttemptStatus`、`DailyAuditAttempt`、`DailyAuditWatermarkV2`；`RuntimeAuditArtifactStore.write_attempt`、`write_daily_report`、`mark_day_completed`、`last_completed_date`。

- [ ] **Step 1: 写内部 JSON 与唯一 Markdown 的 RED 测试**

```python
def test_daily_artifacts_keep_codex_json_internal_and_publish_one_markdown(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    attempt = DailyAuditAttempt(
        attempt_id="runtime_audit_20260806_0015",
        audit_date=date(2026, 8, 5),
        status="succeeded",
        bundle_path="/tmp/bundle.json",
        codex_output_path="/tmp/codex.json",
    )
    attempt_path = store.write_attempt(attempt)
    report_path = store.write_daily_report(date(2026, 8, 5), "# 日报", replace=True)
    assert attempt_path == store.state_dir / "attempts" / f"{attempt.attempt_id}.json"
    assert store.codex_json_path(attempt.attempt_id).parent == store.state_dir / "attempts"
    assert report_path == store.reports_dir / "2026-08-05.md"
    assert list(store.reports_dir.glob("*.json")) == []
```

再写失败重跑不覆盖已有成功日报：

```python
def test_failed_rerun_does_not_replace_successful_daily_report(tmp_path: Path) -> None:
    store = RuntimeAuditArtifactStore(tmp_path / "runtime_audit")
    path = store.write_daily_report(date(2026, 8, 5), "成功日报", replace=True)
    store.write_failed_daily_report_if_absent(date(2026, 8, 5), "失败日报")
    assert path.read_text(encoding="utf-8").strip() == "成功日报"
```

- [ ] **Step 2: 运行 RED，确认旧 store 仍把 Codex JSON 放在 reports**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py \
  -k 'daily_artifacts or failed_rerun'
```

Expected: import/method assertion failure。

- [ ] **Step 3: 定义 daily artifact models**

```python
DailyAttemptStatus = Literal["running", "succeeded", "failed"]


class DailyAuditAttempt(BaseModel):
    schema_version: Literal["assistant_agent_daily_audit_attempt_v1"] = (
        "assistant_agent_daily_audit_attempt_v1"
    )
    attempt_id: str
    audit_date: date
    status: DailyAttemptStatus
    bundle_path: str
    codex_output_path: str | None = None
    error_summary: str | None = None


class DailyAuditWatermarkV2(BaseModel):
    schema_version: Literal["assistant_agent_runtime_audit_watermark_v2"] = (
        "assistant_agent_runtime_audit_watermark_v2"
    )
    last_completed_date: date
    last_attempt_id: str
    bundle_path: str
```

- [ ] **Step 4: 调整 store 路径和原子发布规则**

实现：

```python
self.attempts_dir = self.state_dir / "attempts"
self.schemas_dir = self.state_dir / "schemas"
self.issues_path = self.state_dir / "issues.json"
self.latest_bundle_path = self.state_dir / "latest-bundle.json"

def codex_json_path(self, attempt_id: str) -> Path:
    return self.attempts_dir / f"{attempt_id}.codex.json"

def daily_report_path(self, audit_date: date) -> Path:
    return self.reports_dir / f"{audit_date.isoformat()}.md"
```

`write_daily_report(audit_date, markdown, replace=True)` 使用 `_atomic_write`；`write_failed_daily_report_if_absent` 只在目标不存在时写。`write_bundle` 不再推进 daily watermark，只原子更新 `latest-bundle.json`，供高级 `collect/report` 定位最近 bundle；`mark_day_completed` 才写 v2 watermark。`last_completed_date()` 对 v1 watermark 返回 `None`，确保首次 daily run 只审计昨日；不删除 v1 文件。`cli._resolve_bundle_path` 优先读取 `latest-bundle.json`，迁移期可回退旧 v1 watermark。

- [ ] **Step 5: 运行 Task 2 测试并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py

git add src/assistant_agent/observability/runtime_audit/daily_models.py \
  src/assistant_agent/observability/runtime_audit/cli.py \
  src/assistant_agent/observability/runtime_audit/storage.py \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py
git commit -m "feat(observability): separate daily reports from audit state"
```

---

### Task 3: 问题状态注册表与合法转换

**Files:**
- Create: `src/assistant_agent/observability/runtime_audit/issues.py`
- Modify: `src/assistant_agent/observability/runtime_audit/daily_models.py`
- Modify: `src/assistant_agent/observability/runtime_audit/storage.py`
- Modify: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`

**Interfaces:**
- Produces: `IssueStatus`、`DailyAuditIssue`、`IssueRegistry`、`merge_issue_registry(previous, observed, audit_date)`。
- Consumes: Codex 输出中的 `trace_evidence_refs`、`code_evidence_refs`、`runtime_verification_refs`。

- [ ] **Step 1: 写状态转换 RED 测试**

```python
def test_issue_requires_runtime_evidence_before_verified() -> None:
    previous = IssueRegistry(issues={
        "tool.email_for_market_data": DailyAuditIssue(
            issue_key="tool.email_for_market_data",
            status="open",
            title="错误使用邮件搜索",
            first_seen=date(2026, 8, 5),
            last_seen=date(2026, 8, 5),
            trace_evidence_refs=["trace:bad"],
        )
    })
    addressed = previous.issues["tool.email_for_market_data"].model_copy(update={
        "status": "code_addressed",
        "code_evidence_refs": ["commit:abc123", "test:tests/tdd/tool/test_market.py"],
    })
    merged = merge_issue_registry(previous, [addressed], date(2026, 8, 6))
    assert merged.issues[addressed.issue_key].status == "code_addressed"

    invalid = addressed.model_copy(update={"status": "runtime_verified"})
    with pytest.raises(ValueError, match="runtime verification evidence"):
        merge_issue_registry(merged, [invalid], date(2026, 8, 7))
```

再覆盖带后续 Trace 的 `runtime_verified` 和再次出现的 `regressed`。

- [ ] **Step 2: 运行 RED，确认 issue models 尚不存在**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py -k issue
```

- [ ] **Step 3: 实现 issue models 和转换守卫**

```python
IssueStatus = Literal["open", "code_addressed", "runtime_verified", "regressed", "uncertain"]


class DailyAuditIssue(BaseModel):
    issue_key: str
    status: IssueStatus
    title: str
    plain_summary: str = ""
    user_impact: str = ""
    suggested_change: str = ""
    validation: str = ""
    first_seen: date
    last_seen: date
    trace_evidence_refs: list[str] = Field(default_factory=list)
    code_evidence_refs: list[str] = Field(default_factory=list)
    runtime_verification_refs: list[str] = Field(default_factory=list)


class IssueRegistry(BaseModel):
    schema_version: Literal["assistant_agent_runtime_audit_issues_v1"] = (
        "assistant_agent_runtime_audit_issues_v1"
    )
    issues: dict[str, DailyAuditIssue] = Field(default_factory=dict)
```

守卫规则：`code_addressed` 要求 `code_evidence_refs`；`runtime_verified` 要求 `runtime_verification_refs`；`regressed` 要求新的 `trace_evidence_refs`；没有新观察时保留原状态，不自动关闭。

- [ ] **Step 4: 增加 store 的 issue registry 原子读写**

```python
def read_issue_registry(self) -> IssueRegistry:
    if not self.issues_path.exists():
        return IssueRegistry()
    return IssueRegistry.model_validate_json(self.issues_path.read_text(encoding="utf-8"))

def write_issue_registry(self, registry: IssueRegistry) -> Path:
    _atomic_write(self.issues_path, registry.model_dump_json(indent=2))
    return self.issues_path
```

- [ ] **Step 5: 运行 Task 3 测试并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py -k issue

git add src/assistant_agent/observability/runtime_audit/issues.py \
  src/assistant_agent/observability/runtime_audit/daily_models.py \
  src/assistant_agent/observability/runtime_audit/storage.py \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py
git commit -m "feat(observability): track daily audit issue lifecycle"
```

---

### Task 4: 通俗中文 Codex 契约与日报 renderer

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/daily_models.py`
- Modify: `src/assistant_agent/observability/runtime_audit/runner.py`
- Modify: `src/assistant_agent/observability/runtime_audit/report.py`
- Modify: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`
- Modify: `tests/tdd/runtime_audit/test_runtime_audit.py`

**Interfaces:**
- Produces: `DailyCodexAuditReport`、`render_daily_codex_report`、`render_empty_daily_report`、`render_failed_daily_report`。
- Consumes: Task 3 `DailyAuditIssue` 与现有 `RuntimeAuditBundle`。

- [ ] **Step 1: 写中文报告结构和空日不调用 Codex 的 renderer RED 测试**

```python
def test_human_daily_report_is_chinese_and_moves_machine_ids_to_appendix() -> None:
    report = DailyCodexAuditReport(
        audit_date=date(2026, 8, 5),
        daily_summary="昨天有一个工具选择问题需要决定。",
        activity_summary="共 4 次对话，其中 1 次调用工具。",
        issues=[DailyAuditIssue(
            issue_key="tool.email_for_market_data",
            status="open",
            title="错误使用邮件搜索",
            plain_summary="助手把公开市场查询交给了邮箱搜索。",
            user_impact="用户可能得到无关结果。",
            suggested_change="收紧邮箱搜索的适用范围。",
            validation="等待后续同类自然请求。",
            first_seen=date(2026, 8, 5),
            last_seen=date(2026, 8, 5),
            trace_evidence_refs=["trace:abc/observation:def"],
        )],
        memory_summary="未发现需要处理的记忆问题。",
        infrastructure_summary="Trace 导出正常。",
    )
    markdown = render_daily_codex_report(report)
    assert "## 需要你决定" in markdown
    assert "用户可能得到无关结果" in markdown
    assert markdown.index("## 证据附录") < markdown.index("trace:abc")
    assert "Executive Summary" not in markdown


def test_empty_day_report_is_short_and_explicitly_successful() -> None:
    markdown = render_empty_daily_report(
        date(2026, 8, 5),
        langfuse_available=True,
        local_available=True,
    )
    assert "昨日无可审计对话" in markdown
    assert "审计任务运行正常" in markdown
```

- [ ] **Step 2: 运行 RED，确认旧 renderer 仍使用英文技术标题**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py -k 'human_daily or empty_day'
```

- [ ] **Step 3: 定义新的 structured report**

```python
class DailyCodexAuditReport(BaseModel):
    schema_version: Literal["assistant_agent_daily_codex_audit_v1"] = (
        "assistant_agent_daily_codex_audit_v1"
    )
    audit_date: date
    daily_summary: str
    activity_summary: str
    issues: list[DailyAuditIssue] = Field(default_factory=list)
    memory_summary: str
    infrastructure_summary: str
    limitations: list[str] = Field(default_factory=list)
    production_mutation_allowed: Literal[False] = False
```

继续使用 `_make_object_schemas_strict`，使所有 object `additionalProperties=false` 且字段全部 required。

- [ ] **Step 4: 修改 Codex prompt，要求普通中文与状态证据**

Prompt 必须包含以下明确约束：

```text
报告读者是项目维护者，不是另一个 Codex。
先用普通中文解释用户会感受到什么，再说明维护者需要决定什么。
机器 ID 只进入 evidence refs，不在正文堆砌。
代码变化只能标记 code_addressed；没有后续真实 Trace 不得标记 runtime_verified。
同一个 code_addressed 问题不得每天重复完整修改建议。
不得运行测试、修改文件、调用网络、Provider、Tool、Memory 或其他 agent。
production_mutation_allowed 必须为 false。
```

runner 接收 attempt input path 和 prior issue state，将二者作为路径写入 stdin prompt，不把 bundle 放进 argv。新增接口固定为：

```python
def run_daily_codex_report(
    *,
    audit_date: date,
    bundle_path: Path,
    issues_path: Path,
    repo_root: Path,
    output_path: Path,
    schema_path: Path,
    environment: Mapping[str, str] | None = None,
    timeout_seconds: float = 900.0,
    process_runner: Callable[..., Any] = subprocess.run,
) -> DailyCodexAuditReport
```

实现沿用现有 `run_codex_report` 的 schema 写入、sanitized environment、固定 read-only command、return code 检查、Pydantic JSON 校验和 audit date 匹配；唯一差异是使用 `DailyCodexAuditReport` schema，并在 stdin prompt 中同时引用 `bundle_path` 与 `issues_path`。

保留现有 `run_codex_report` 供显式 rolling-window/legacy report 使用；新增 `run_daily_codex_report` 承载 daily schema 和 issue state，避免把两种输出契约揉成条件分支。两种 runner 的 JSON 都写入 `state/attempts/`，新运行不再向 `reports/` 写 JSON。

- [ ] **Step 5: 实现三个中文 renderer**

`render_daily_codex_report` 按规格输出“一句话结论、昨日概况、需要你决定、已处理等待自然验证、昨日已验证解决、记忆情况、系统运行情况、证据附录”。`render_empty_daily_report` 不调用 Codex；`render_failed_daily_report` 明确“审计未完成”，并使用清洗后的错误摘要。

- [ ] **Step 6: 运行 runner/schema 与 renderer 回归并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py \
  tests/tdd/runtime_audit/test_runtime_audit.py \
  -k 'codex or report or schema or runner or empty_day or human_daily'

git add src/assistant_agent/observability/runtime_audit/daily_models.py \
  src/assistant_agent/observability/runtime_audit/runner.py \
  src/assistant_agent/observability/runtime_audit/report.py \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py \
  tests/tdd/runtime_audit/test_runtime_audit.py
git commit -m "feat(observability): render plain-language daily audit reports"
```

---

### Task 5: Daily orchestration、补跑与失败降级

**Files:**
- Create: `src/assistant_agent/observability/runtime_audit/daily_runner.py`
- Modify: `src/assistant_agent/observability/runtime_audit/cli.py`
- Modify: `src/assistant_agent/observability/runtime_audit/storage.py`
- Modify: `tests/tdd/runtime_audit/test_daily_runtime_audit.py`

**Interfaces:**
- Consumes: Tasks 1–4 的 window、store、issue registry、Codex runner 和 renderer。
- Produces: `run_pending_daily_audits -> list[DailyAuditRunResult]`、`run_one_daily_audit -> DailyAuditRunResult`。

- [ ] **Step 1: 写完整自动链的 RED 测试**

使用注入的 `FakeLangfuseSource` 和 `fake_codex_runner`，断言：

```python
def test_nonempty_daily_run_collects_invokes_codex_and_publishes_markdown(tmp_path: Path) -> None:
    calls = []
    result = run_one_daily_audit(
        window=window_for_date(date(2026, 8, 5)),
        source=FakeLangfuseSource([trace_fixture()]),
        local_trace_path=tmp_path / "graph_trace.jsonl",
        store=RuntimeAuditArtifactStore(tmp_path / "runtime_audit"),
        codex_runner=lambda **kwargs: calls.append(kwargs) or daily_report_fixture(),
        collected_at=datetime(2026, 8, 6, 0, 15, tzinfo=timezone.utc),
    )
    assert result.status == "succeeded"
    assert len(calls) == 1
    assert result.report_path.name == "2026-08-05.md"
    assert result.report_path.exists()
```

再覆盖：空日 `calls == []`；Langfuse 失败写故障日报；Codex 失败不更新 issues/watermark；补跑第 N 日失败后不跨越该日。

- [ ] **Step 2: 运行 RED，确认 orchestrator 尚不存在**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py \
  -k 'daily_run or backfill'
```

- [ ] **Step 3: 实现单日执行顺序**

先在 `daily_runner.py` 定义可供 CLI 和测试消费的结果：

```python
class DailyAuditRunResult(BaseModel):
    audit_date: date
    status: Literal["succeeded", "failed"]
    attempt_id: str
    bundle_path: Path
    report_path: Path | None = None
    error_summary: str | None = None
```

`run_one_daily_audit` 固定签名和顺序：

```python
def run_one_daily_audit(
    *,
    window: DailyAuditWindow,
    source: LangfuseAuditSource,
    local_trace_path: Path,
    store: RuntimeAuditArtifactStore,
    repo_root: Path,
    codex_runner: Callable[..., DailyCodexAuditReport],
    collected_at: datetime,
    judge_grace: timedelta = timedelta(minutes=15),
    low_score_threshold: float = 0.5,
) -> DailyAuditRunResult
```

函数先以 `window.start_utc/end_utc` 调用 `collect_runtime_audit`，随后写 bundle 和 running attempt。Langfuse 不可读时只在没有成功日报的情况下发布失败日报，attempt 标记 failed 且不推进 watermark。真正空日发布 empty report 并完成 watermark。其他情况调用 `codex_runner`，以 `merge_issue_registry(store.read_issue_registry(), report.issues, window.audit_date)` 生成新 registry，再按“日报、registry、succeeded attempt、watermark”的顺序原子持久化。

状态写入顺序确保：失败不会覆盖成功日报，watermark 只在日报和内部状态全部成功后前移。

- [ ] **Step 4: 实现多日补跑与 CLI 返回码**

`run_pending_daily_audits` 使用 `pending_audit_dates` 顺序处理；某日失败立即停止。CLI：全部完成返回 0；任何日期失败返回 2；stdout 最终输出包含 `audit_dates`、`report_paths`、`failed_date` 的 JSON，不打印凭据或 Trace 正文。

`run --date` 只处理指定日并允许刷新同一日报；无参数 `run` 才按 watermark 补跑。`--dry-run` 只输出将处理的日期和路径，不读取 Langfuse、不调用 Codex。

- [ ] **Step 5: 运行整个 daily TDD 并提交**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py

git add src/assistant_agent/observability/runtime_audit/daily_runner.py \
  src/assistant_agent/observability/runtime_audit/cli.py \
  src/assistant_agent/observability/runtime_audit/storage.py \
  tests/tdd/runtime_audit/test_daily_runtime_audit.py
git commit -m "feat(observability): automate daily runtime audits"
```

---

### Task 6: 每日 timer、权威文档与完整验证

**Files:**
- Modify: `deploy/systemd/user/assistant-agent-runtime-audit.timer`
- Modify: `deploy/systemd/user/assistant-agent-runtime-audit.service`
- Modify: `docs/observability-harness.md`
- Modify: `scripts/README.md`

**Interfaces:**
- Consumes: Task 5 无参数 `scripts/run_runtime_audit.py run`。
- Produces: 每日 00:15 user timer 和最终 operator 文档。

- [ ] **Step 1: 修改 timer/service**

timer：

```ini
[Unit]
Description=Run Assistant Agent runtime audit daily

[Timer]
OnCalendar=*-*-* 00:15:00 Asia/Shanghai
Persistent=true
RandomizedDelaySec=2min
Unit=assistant-agent-runtime-audit.service

[Install]
WantedBy=timers.target
```

service 保持 mock mode、显式 Codex executable 和 read-only hardening，`ExecStart` 继续调用无参数 `run`。

- [ ] **Step 2: 更新当前权威文档**

在 `docs/observability-harness.md` 替换“最近两小时/每小时”和旧 artifact 表，写清：自然日、补跑、空日、内部 JSON、人读 Markdown、状态机、失败降级和手工 `--date`。在 `scripts/README.md` 只更新稳定入口摘要，不复制完整参数列表。

- [ ] **Step 3: 运行完整定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit \
  tests/tdd/mem0-langfuse-visualization

/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/observability/runtime_audit \
  scripts/run_runtime_audit.py

systemd-analyze --user verify \
  deploy/systemd/user/assistant-agent-runtime-audit.service \
  deploy/systemd/user/assistant-agent-runtime-audit.timer

/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/run_runtime_audit.py --no-env-file run --dry-run

git diff --check
```

Expected: pytest 全部通过；compileall、systemd verify、diff check 退出 0；dry-run 输出前一自然日、`codex_enabled=true` 和 `production_mutation_allowed=false`，且不联网。

- [ ] **Step 4: 在 operator 环境 reload timer，不立即触发真实日报**

```bash
systemctl --user daemon-reload
systemctl --user restart assistant-agent-runtime-audit.timer
systemctl --user is-enabled assistant-agent-runtime-audit.timer
systemctl --user is-active assistant-agent-runtime-audit.timer
systemctl --user list-timers assistant-agent-runtime-audit.timer --no-legend
```

Expected: timer 为 enabled/active，下次触发在北京时间次日 00:15 附近；此步骤不手工 start service，不调用真实 Codex。

- [ ] **Step 5: 提交最终调度和文档**

```bash
git add deploy/systemd/user/assistant-agent-runtime-audit.service \
  deploy/systemd/user/assistant-agent-runtime-audit.timer \
  docs/observability-harness.md scripts/README.md
git commit -m "docs(observability): operate runtime audit as a daily review"
```

## 完成标准

- 无人工命令时，timer 每天 00:15 审计前一自然日，并补齐 watermark 之后漏掉的日期。
- 有 Trace 的日期自动调用只读 Codex；无 Trace 的日期生成极简中文成功日报且不调用 Codex。
- `reports/` 对新运行只产生 `YYYY-MM-DD.md`；内部 JSON 不出现在人读目录。
- 日报正文以普通中文解释影响和建议，机器 ID 集中到证据附录。
- 当天代码变化但无复测 Trace 时状态只能是 `code_addressed`；后续自然 Trace 才能升级为 `runtime_verified` 或 `regressed`。
- Langfuse/Codex/Schema 失败不会覆盖已有成功日报，也不会推进 issue registry 或 watermark。
- 全部定向 pytest、compileall、systemd verify、dry-run 和 `git diff --check` 通过。
