# Runtime Audit Bundle 压缩实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将全天 Runtime Audit bundle 压缩为无 raw metadata、共享 Tool catalog、紧凑序列化且向后兼容的 v2 契约。

**Architecture:** Langfuse source model 继续暂存 metadata，collector 先完成确定性分类，再调用独立的纯压缩函数生成持久化快照。v2 bundle 顶层保存按完整 SHA-256 标识的 Tool catalog，Trace/Observation input 只保留引用；storage 只对 inbox bundle 使用紧凑、`exclude_none` 序列化。读取端同时接受 v1 与 v2。

**Tech Stack:** Python 3.12、Pydantic v2、标准库 `hashlib/json/copy`、pytest、Ruff。

## Global Constraints

- 不修改 Langfuse、Memory、Agent runtime、canonical local event 或原生 evaluator。
- raw metadata 只能用于收集期确定性分类，不得进入新写 v2 bundle。
- 只改写 Trace/Observation `input` 中值为 list 的 `tools`，不得改写 output 中同名业务字段。
- Tool catalog ID 使用完整 SHA-256，引用必须能在同一 bundle 中解析。
- `report --bundle <v1文件>` 必须继续可读。
- pytest 使用 mock/offline，不读取真实 `.env`，不调用 Langfuse、Codex 或 Provider。
- Core invariant: unchanged；测试只更新临时 `tests/tdd/runtime_audit`。

---

### Task 1: v2 bundle 契约与纯压缩函数

**Files:**
- Create: `src/assistant_agent/observability/runtime_audit/bundle_compaction.py`
- Modify: `src/assistant_agent/observability/runtime_audit/models.py`
- Test: `tests/tdd/runtime_audit/test_bundle_compaction.py`

**Interfaces:**
- Consumes: `list[LangfuseTraceSnapshot]`。
- Produces: `compact_trace_evidence(traces: list[LangfuseTraceSnapshot]) -> tuple[list[LangfuseTraceSnapshot], dict[str, list[Any]]]`。
- Produces: `RuntimeAuditBundle.tool_catalogs: dict[str, list[Any]]`；默认写 v2，同时读取 v1。

- [ ] **Step 1: 写 v2 RED 测试**

构造两个 Observation：嵌套 `input.tools` 相同、metadata 不同，output 也包含业务 `tools`：

```python
def test_compaction_removes_metadata_and_reuses_catalog_without_touching_output():
    traces = [_trace_with_repeated_catalogs_and_output_tools()]
    compacted, catalogs = compact_trace_evidence(traces)
    first, second = compacted[0].observations

    assert len(catalogs) == 1
    assert compacted[0].metadata is None
    assert first.metadata is None
    assert compacted[0].scores[0].metadata is None
    assert first.input["request"]["tool_catalog_ref"] == second.input["tool_catalog_ref"]
    assert "tools" not in first.input["request"]
    assert first.output["tools"] == ["business-result"]
    assert traces[0].observations[0].metadata is not None
```

同文件覆盖 v1 可读、v2 悬空引用拒绝、四种 catalog 得到四个 64 位小写摘要。

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_bundle_compaction.py
```

预期：因模块和 v2 字段不存在而 collection error。

- [ ] **Step 3: 实现最小纯函数与模型校验**

```python
def compact_trace_evidence(
    traces: list[LangfuseTraceSnapshot],
) -> tuple[list[LangfuseTraceSnapshot], dict[str, list[Any]]]:
    catalogs: dict[str, list[Any]] = {}
    compacted = []
    for trace in traces:
        observations = [
            observation.model_copy(
                update={
                    "input": _replace_input_tool_catalogs(observation.input, catalogs),
                    "metadata": None,
                },
                deep=True,
            )
            for observation in trace.observations
        ]
        scores = [score.model_copy(update={"metadata": None}, deep=True) for score in trace.scores]
        compacted.append(trace.model_copy(update={
            "input": _replace_input_tool_catalogs(trace.input, catalogs),
            "metadata": None,
            "observations": observations,
            "scores": scores,
        }, deep=True))
    return compacted, catalogs
```

`_replace_input_tool_catalogs` 使用 `copy.deepcopy`，只替换 input dict 中 `tools: list`；同层已有 `tool_catalog_ref` 时拒绝。catalog canonical bytes 固定为：

```python
json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

在 `models.py` 增加 v1/v2 `Literal`、`tool_catalogs` 和 `model_validator(mode="after")`。v2 遍历 Trace/Observation input 的引用，拒绝非 64 位小写十六进制或悬空引用；v1 不强制。

- [ ] **Step 4: 运行 GREEN 与 Ruff**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit/test_bundle_compaction.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/observability/runtime_audit/bundle_compaction.py \
  src/assistant_agent/observability/runtime_audit/models.py \
  tests/tdd/runtime_audit/test_bundle_compaction.py
```

- [ ] **Step 5: 提交 Task 1**

```bash
git add src/assistant_agent/observability/runtime_audit/bundle_compaction.py \
  src/assistant_agent/observability/runtime_audit/models.py \
  tests/tdd/runtime_audit/test_bundle_compaction.py
git commit -m "feat(observability): compact runtime audit evidence"
```

---

### Task 2: Collector、storage 与 Codex prompt 接入

**Files:**
- Modify: `src/assistant_agent/observability/runtime_audit/collector.py`
- Modify: `src/assistant_agent/observability/runtime_audit/storage.py`
- Modify: `src/assistant_agent/observability/runtime_audit/runner.py`
- Test: `tests/tdd/runtime_audit/test_bundle_compaction.py`

**Interfaces:**
- Consumes: Task 1 的 `compact_trace_evidence(traces)` 与 v2 `RuntimeAuditBundle`。
- Produces: collector 返回压缩 v2 bundle，storage 写紧凑 JSON，Codex prompt 解释 catalog 引用。

- [ ] **Step 1: 写接入 RED 测试**

```python
def test_collector_classifies_metadata_before_persisted_compaction():
    bundle = collect_runtime_audit(
        source=_metadata_driven_source(),
        local_trace_path=None,
        window_start=datetime(2026, 8, 5, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 6, tzinfo=timezone.utc),
        collected_at=datetime(2026, 8, 6, 1, tzinfo=timezone.utc),
    )
    assert _has_expected_tool_and_memory_findings(bundle.findings)
    assert bundle.schema_version == "assistant_agent_runtime_audit_bundle_v2"
    assert all(trace.metadata is None for trace in bundle.traces)
    assert bundle.tool_catalogs

def test_store_writes_compact_bundle_without_none_or_raw_metadata(tmp_path):
    path = RuntimeAuditArtifactStore(tmp_path).write_bundle(_v2_bundle())
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "\n  " not in text
    assert "metadata" not in payload["traces"][0]

def test_daily_prompt_explains_tool_catalog_refs():
    prompt = _daily_codex_prompt(
        audit_date=date(2026, 8, 5),
        bundle_path=Path("/tmp/bundle.json"),
        issues_path=Path("/tmp/issues.json"),
    )
    assert "tool_catalog_ref" in prompt
    assert "tool_catalogs" in prompt
```

- [ ] **Step 2: 运行接入 RED**

运行 Task 1 的 pytest 命令；预期 collector、storage 和 prompt 断言失败。

- [ ] **Step 3: 完成接入**

collector 先把当前内联构造的 `AuditCoverage(...)` 和 `sorted(findings, ...)` 分别赋给
`coverage`、`ordered_findings`，再在生成全部 findings 后执行：

```python
compacted_traces, tool_catalogs = compact_trace_evidence(traces)
return RuntimeAuditBundle(
    audit_run_id=audit_run_id or format_audit_run_id(collected_at),
    collected_at=collected_at,
    window_start=window_start,
    window_end=window_end,
    coverage=coverage,
    traces=compacted_traces,
    local_manifests=manifests,
    local_fallbacks=fallbacks,
    findings=ordered_findings,
    tool_catalogs=tool_catalogs,
    production_mutation_allowed=False,
)
```

`RuntimeAuditArtifactStore.write_bundle` 改为：

```python
_atomic_write(path, bundle.model_dump_json(exclude_none=True))
```

attempt、registry、schema 和 Markdown 不改格式。daily 与 legacy prompt 加入：遇到 `tool_catalog_ref` 必须从 bundle 顶层 `tool_catalogs` 解析，不能把引用当工具名。

- [ ] **Step 4: 运行 GREEN 与 Runtime Audit 定向测试**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit
```

- [ ] **Step 5: 提交 Task 2**

```bash
git add src/assistant_agent/observability/runtime_audit/collector.py \
  src/assistant_agent/observability/runtime_audit/storage.py \
  src/assistant_agent/observability/runtime_audit/runner.py \
  tests/tdd/runtime_audit/test_bundle_compaction.py
git commit -m "feat(observability): persist compact daily audit bundles"
```

---

### Task 3: 文档、兼容回归与体积验证

**Files:**
- Modify: `docs/observability-harness.md`
- Modify: `tests/tdd/runtime_audit/test_bundle_compaction.py`
- Verify: `tests/tdd/mem0-langfuse-visualization/`

**Interfaces:**
- Consumes: 已接入的 v2 bundle 契约。
- Produces: 权威架构说明、v1/v2 回归与最终验证记录。

- [ ] **Step 1: 增加合成体积回归**

构造 49 个 Observation、4 种 catalog、重复比例 30/14/3/2：

```python
assert len(bundle.tool_catalogs) == 4
assert compact_size < pretty_raw_size * 0.40
assert RuntimeAuditBundle.model_validate_json(v1_json).schema_version.endswith("_v1")
```

阈值只约束刻意构造的 fixture，不绑定生产 trace 数量。

- [ ] **Step 2: 更新权威文档**

在 `docs/observability-harness.md` 的 Runtime 审计段落写明：collector 先使用 raw metadata 分类，再删除 metadata；v2 使用顶层 `tool_catalogs` 与 input `tool_catalog_ref`；inbox 是紧凑内部 JSON，v1 仅兼容读取。

- [ ] **Step 3: 完整定向验证**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/runtime_audit tests/tdd/mem0-langfuse-visualization
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/observability/runtime_audit \
  tests/tdd/runtime_audit/test_bundle_compaction.py
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m compileall -q \
  src/assistant_agent/observability/runtime_audit
git diff --check
```

- [ ] **Step 4: 已备份真实 bundle 的只读体积测量**

只读解析 `.data/runtime_audit.backup-20260806-153753/inbox/` 中一个全天 bundle，在内存打印原始/新字节、catalog 出现次数与唯一数；不得覆盖备份或写 `.data`，不得调用 Langfuse/Codex。

- [ ] **Step 5: 提交 Task 3**

```bash
git add docs/observability-harness.md tests/tdd/runtime_audit/test_bundle_compaction.py
git commit -m "docs(observability): document compact audit bundles"
```

最终汇报：

```text
Core invariant: unchanged.
Tests: added tests/tdd/runtime_audit/test_bundle_compaction.py for temporary RED/GREEN; user may delete tests/tdd/runtime_audit manually.
```
