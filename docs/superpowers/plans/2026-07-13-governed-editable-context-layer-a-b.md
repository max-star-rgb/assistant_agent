# Governed Editable Context Layer A+B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变默认 Provider 请求、工具治理或 MemoryManager 权威边界的前提下，引入 `ContextSection v1` 与显式 local opt-in 的 `SoulContextSource`，并把一次 run 冻结的 owner persona 安全编译进 system instruction。

**Architecture:** `AgentGraphRuntime` 在创建 `AgentState` 后通过 `ContextSourceCoordinator` 加载一次受治理 source，并把 prompt-safe `ContextSourceResult` 冻结到 state。`build_assistant_context_pack` 只消费结构化 section、执行全局预算，`PromptCompiler` 只把已验证的唯一 SOUL section 作为 `owner_persona` 交给 system prompt policy；loader、builder 和 compiler 之间不共享文件路径或可执行回调。

**Tech Stack:** Python 3.11、Pydantic v2、dataclass、pathlib/os/stat、pytest；不新增依赖，不调用真实 Provider。

## Global Constraints

- 实现范围只包含设计规格的 A+B：`ContextSection v1`、source protocol/coordinator、local opt-in SOUL、pack/state/runtime/compiler/report 集成、测试与权威文档。
- `MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED` 默认 `false`。
- `MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT` 默认 `.local/context`。
- `MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID` 必须显式设置；不得从 request metadata 推导或回退。
- SOUL 文件上限固定为 16,000 bytes、4,000 chars；编译 section 上限 2,000 chars；每个固定 subsection 上限 800 chars；每次结果最多保留 16 个 issue。
- 固定 subsection 及预算顺序为 `Relationship Boundaries -> Avoid -> Persona -> Expression Style`。
- 文件能力默认关闭；关闭时不得触碰文件系统，默认 system instruction 必须逐字节保持现状。
- MemoryManager、MemoryReadPolicy、MemoryWritePolicy、ActionValidator、ToolExecutor、ToolRegistry 和 Provider adapter 职责不变。
- 不修改 memory tool/store、Gateway/realtime 协议、Provider cache hint、`.codex/skills/**` 或业务 `skills/**`。
- 不新增依赖、不联网、不运行真实 Provider smoke。
- 当前 `cqy` 工作区已有无关修改；只编辑本计划列出的文件，不回滚、不格式化、不提交无关文件。
- 根据仓库 `AGENTS.md`，设计、计划、代码、测试和权威文档作为同一阶段材料保留；本次不自动执行 git commit。

---

## File Structure

### Create

- `src/assistant_agent/services/context/sources.py`：source request/protocol、coordinator、result invariant 与 issue cap。
- `src/assistant_agent/services/context/soul_source.py`：固定路径读取、Markdown 解析、安全验证、预算编译、进程内 last-known-good。
- `tests/test_context_sources.py`：contract、coordinator invariant、identity/config、runtime load-once。
- `tests/test_soul_context_source.py`：文件安全、解析、预算、版本变化和 last-known-good。

### Modify

- `src/assistant_agent/schemas/context.py`：section/source/report Pydantic contract、pack/budget additive 字段。
- `src/assistant_agent/config.py`：三个固定环境变量对应的配置字段。
- `src/assistant_agent/agent/state.py`：冻结的 `context_source_result`。
- `src/assistant_agent/agent/runtime.py`：构造 coordinator；每个 run 加载一次。
- `src/assistant_agent/services/context/builder.py`：消费 frozen sections、persona-first budget、source counts。
- `src/assistant_agent/agent/system_prompt_policy.py`：keyword-only `owner_persona` 与不可覆盖边界。
- `src/assistant_agent/services/context/prompt_compiler.py`：从 pack 选择唯一 SOUL section。
- `src/assistant_agent/services/context/report.py`：非累加的 source summary。
- `src/assistant_agent/services/context/observability.py`：trace 中加入 redacted source summary。
- `tests/test_provider_config_validation.py`：默认值与固定 env 映射。
- `tests/unit/test_agent_state.py`：additive state round-trip。
- `tests/test_system_prompt_policy.py`：默认字节等价与 persona 排序。
- `tests/test_prompt_compiler.py`：三种 compile mode 的 persona 和工具契约。
- `tests/test_assistant_context_renderer.py`：pack/budget/report source accounting。
- `tests/test_native_runtime_system_prompt_policy.py`：真实 runtime load-once 与跨 user fail-closed。
- `docs/context_engineering_status.md`：记录已实现边界、配置、限制和关键文件。
- `docs/memory-service-architecture.md`：补充 USER/MEMORY 文件仍只是未来 projection，不是 durable truth。

### Explicitly not created

- `docs/prompt-engineering-architecture.md` 当前不存在，本计划不凭空创建另一份权威文档。
- `.local/context/SOUL.md` 不进入仓库，由本机用户显式创建。

---

### Task 1: Add ContextSection v1 and explicit configuration

**Files:**

- Modify: `src/assistant_agent/schemas/context.py`
- Modify: `src/assistant_agent/config.py`
- Modify: `src/assistant_agent/agent/state.py`
- Test: `tests/test_context_sources.py`
- Test: `tests/test_provider_config_validation.py`
- Test: `tests/unit/test_agent_state.py`

**Interfaces:**

- Produces: `ContextSection`, `ContextSourceIssue`, `ContextSourceResult`, `ContextSourceReport`, and strict Literal aliases.
- Produces: `ProviderConfig.editable_context_enabled: bool`, `editable_context_root: str`, `editable_context_user_id: str | None`.
- Produces: `AgentState.context_source_result: ContextSourceResult` and `AssistantContextPack.context_sections: list[ContextSection]`.
- Produces: `ContextBudgetReport.owner_persona_chars: int` and `ContextReport.context_sources: ContextSourceReport`.

- [x] **Step 1: Write failing contract and config tests**

Add strict tests with these assertions:

```python
def test_context_section_contract_is_strict_and_serializable() -> None:
    section = ContextSection(
        section_id="owner.soul",
        kind="soul",
        title="Owner persona",
        content="保持简洁。",
        authority="owner_persona",
        stability="semi_stable",
        source_type="editable_file",
        source_ref="editable_context:soul",
        identity_scope="local_owner",
        max_chars=2_000,
    )
    assert ContextSection.model_validate_json(section.model_dump_json()) == section


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("section_id", ""),
        ("authority", "unknown"),
        ("stability", "mutable"),
        ("max_chars", -1),
    ],
)
def test_context_section_rejects_invalid_contract(field: str, value: object) -> None:
    payload = valid_context_section_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        ContextSection.model_validate(payload)


def test_editable_context_config_defaults_closed() -> None:
    config = ProviderConfig.from_env({})
    assert config.editable_context_enabled is False
    assert config.editable_context_root == ".local/context"
    assert config.editable_context_user_id is None


def test_editable_context_config_uses_only_fixed_env_names() -> None:
    config = ProviderConfig.from_env(
        {
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED": "true",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT": "/tmp/local-context",
            "MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID": "owner-1",
        }
    )
    assert config.editable_context_enabled is True
    assert config.editable_context_root == "/tmp/local-context"
    assert config.editable_context_user_id == "owner-1"
```

Extend `test_agent_state_serializes_and_deserializes` with one valid section inside `ContextSourceResult` and assert the restored state preserves the result. Also assert constructing `AssistantContextPack` without `context_sections` produces `[]`.

- [x] **Step 2: Run RED verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_context_sources.py \
  tests/test_provider_config_validation.py \
  tests/unit/test_agent_state.py -q
```

Expected: collection or assertion failures because the new contracts and config fields do not exist.

- [x] **Step 3: Implement strict additive contracts**

Add these aliases and models to `schemas/context.py`:

```python
ContextAuthority = Literal[
    "system_policy",
    "owner_persona",
    "procedural_guidance",
    "user_profile_data",
    "user_history_evidence",
    "session_state",
    "runtime_evidence",
    "tool_contract",
]
ContextStability = Literal["invariant", "semi_stable", "volatile"]
ContextSectionKind = Literal[
    "soul",
    "user_profile",
    "core_memory",
    "skill_index",
    "skill_body",
    "skill_reference",
    "session_summary",
    "recent_transcript",
    "retrieved_memory",
    "realtime_task_state",
    "plan_state",
    "tool_observation",
    "tool_schema",
    "tool_capability",
]
ContextSourceType = Literal[
    "runtime",
    "editable_file",
    "memory_service",
    "skill_loader",
    "tool_registry",
]
ContextIdentityScope = Literal["runtime", "local_owner", "user", "project", "tenant"]


class ContextSection(BaseModel):
    schema_version: Literal["context_section_v1"] = "context_section_v1"
    section_id: str = Field(min_length=1)
    kind: ContextSectionKind
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    authority: ContextAuthority
    stability: ContextStability
    source_type: ContextSourceType
    source_ref: str = ""
    source_version: str = ""
    identity_scope: ContextIdentityScope = "runtime"
    priority: int = Field(default=100, ge=0)
    max_chars: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    sensitive: bool = False
    notes: list[str] = Field(default_factory=list)


class ContextSourceIssue(BaseModel):
    code: str = Field(min_length=1)
    source_ref: str = ""
    section_id: str | None = None
    recoverable: bool = True
    public_message: str = Field(min_length=1)


class ContextSourceResult(BaseModel):
    sections: list[ContextSection] = Field(default_factory=list)
    issues: list[ContextSourceIssue] = Field(default_factory=list)
    used_last_known_good: bool = False


class ContextSourceReport(BaseModel):
    schema_version: Literal["context_source_report_v1"] = "context_source_report_v1"
    count_by_kind: dict[str, int] = Field(default_factory=dict)
    chars_by_authority: dict[str, int] = Field(default_factory=dict)
    chars_by_stability: dict[str, int] = Field(default_factory=dict)
    source_issue_count: int = Field(default=0, ge=0)
    source_issue_codes: list[str] = Field(default_factory=list)
    used_last_known_good: bool = False
    source_versions_changed: int = Field(default=0, ge=0)
    omitted_section_count: int = Field(default=0, ge=0)
    cache_layout_version: str = "editable_context_v1"
```

Add only defaulted fields to existing models:

```python
ContextBudgetReport.owner_persona_chars = Field(default=0, ge=0)
ContextReport.context_sources = Field(default_factory=ContextSourceReport)
AssistantContextPack.context_sections = Field(default_factory=list)
AgentState.context_source_result = Field(default_factory=ContextSourceResult)
```

Use normal class annotations in the actual source rather than assigning Pydantic fields after class creation.

- [x] **Step 4: Implement fixed environment mapping**

Add the three dataclass fields to `ProviderConfig`, then map them in `from_env` exactly as follows:

```python
editable_context_enabled=_bool_env(
    source.get("MULTIMODAL_AGENT_EDITABLE_CONTEXT_ENABLED"),
    False,
),
editable_context_root=(
    source.get("MULTIMODAL_AGENT_EDITABLE_CONTEXT_ROOT") or ".local/context"
),
editable_context_user_id=(
    source.get("MULTIMODAL_AGENT_EDITABLE_CONTEXT_USER_ID") or None
),
```

Do not add request metadata aliases.

- [x] **Step 5: Run GREEN verification**

Run the Step 2 command. Expected: all selected tests pass.

---

### Task 2: Implement the coordinator and governed SOUL loader

**Files:**

- Create: `src/assistant_agent/services/context/sources.py`
- Create: `src/assistant_agent/services/context/soul_source.py`
- Test: `tests/test_context_sources.py`
- Test: `tests/test_soul_context_source.py`

**Interfaces:**

- Consumes: Task 1 `ContextSection`, `ContextSourceIssue`, `ContextSourceResult`.
- Produces: `ContextSourceRequest`, `ContextSource`, `ContextSourceCoordinator.load_once()`.
- Produces: `SoulContextSource.source_id == "soul"` and `SoulContextSource.load()`.
- Guarantees: one combined SOUL section, unique IDs, no sensitive section, maximum 16 issues.

- [x] **Step 1: Write failing coordinator tests**

Cover a disabled request with a spy source whose `load()` raises if called; identity mismatch; duplicate IDs; sensitive sections; source exceptions; issue capping. The expected coordinator behavior is:

```python
request = ContextSourceRequest(
    user_id="owner-1",
    source_root=tmp_path,
    local_owner_user_id="owner-1",
    runtime_profile="local_demo",
    editable_context_enabled=False,
    section_char_budgets={"soul": 2_000},
    enabled_source_ids={"soul"},
)
result = ContextSourceCoordinator([source]).load_once(request)
assert result == ContextSourceResult()
assert source.call_count == 0
```

For invalid coordinator output, assert stable issue codes `context_source_duplicate_section_id`, `context_source_sensitive_section_rejected`, and `context_source_load_failed`; assert public messages do not contain exception text or paths.

- [x] **Step 2: Write failing SOUL loader tests**

Use `tmp_path` and a helper that writes `SOUL.md`. Cover these exact behaviors:

```python
VALID_SOUL = """## Persona
沉着、直接。

## Expression Style
先给结论，再给必要依据。

## Relationship Boundaries
尊重用户决定，不代替用户确认副作用操作。

## Avoid
避免夸大确定性。
"""
```

- valid file returns one `kind="soul"`, `authority="owner_persona"`, `source_ref="editable_context:soul"` section;
- combined content follows fixed priority order, not source heading order;
- missing file returns `soul_file_missing` without raw path;
- mismatched identity returns `editable_context_identity_mismatch` before any file open;
- missing owner returns `editable_context_owner_unconfigured`;
- unknown heading returns `soul_unknown_section` and does not update last-known-good;
- invalid UTF-8 returns `soul_invalid_utf8`;
- 16,001 bytes returns `soul_file_too_large`;
- more than 4,000 decoded characters returns `soul_content_too_large`;
- secret assignment, bearer token, provider raw marker, long base64, and data URI each return `soul_unsafe_content`;
- directory, FIFO or non-regular target returns `soul_not_regular_file`;
- symlink outside root returns `soul_path_outside_root` or `soul_symlink_not_allowed` without opening target;
- a paragraph over 800 chars is omitted whole, never cut mid-sentence;
- compiled content stays within 2,000 chars and records `selected_paragraphs:<n>` / `omitted_paragraphs:<n>` notes;
- first valid load includes `source_version_changed`, second identical load does not, changed valid load includes it again;
- invalid update reuses the last valid section and sets `used_last_known_good=True`;
- invalid first load returns no section;
- separate `(resolved_root, owner_id)` keys do not share last-known-good.

- [x] **Step 3: Run RED verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_context_sources.py \
  tests/test_soul_context_source.py -q
```

Expected: failures because the source modules do not exist.

- [x] **Step 4: Implement source request/protocol/coordinator**

Use this exact public shape in `sources.py`:

```python
class ContextSourceRequest(BaseModel):
    user_id: str = Field(min_length=1)
    source_root: Path
    local_owner_user_id: str | None = None
    runtime_profile: str = Field(min_length=1)
    editable_context_enabled: bool = False
    section_char_budgets: dict[str, int] = Field(default_factory=dict)
    enabled_source_ids: set[str] = Field(default_factory=set)


class ContextSource(Protocol):
    source_id: str

    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        raise NotImplementedError


class ContextSourceCoordinator:
    def __init__(self, sources: Iterable[ContextSource], *, max_issues: int = 16) -> None:
        self._sources = tuple(sources)
        self._max_issues = max_issues

    def load_once(self, request: ContextSourceRequest) -> ContextSourceResult:
        if not request.editable_context_enabled:
            return ContextSourceResult()
        sections: list[ContextSection] = []
        issues: list[ContextSourceIssue] = []
        used_last_known_good = False
        seen_ids: set[str] = set()
        soul_seen = False
        for source in self._sources:
            if source.source_id not in request.enabled_source_ids:
                continue
            try:
                loaded = source.load(request)
            except Exception:
                issues.append(
                    ContextSourceIssue(
                        code="context_source_load_failed",
                        source_ref=f"editable_context:{source.source_id}",
                        public_message="The editable context source could not be loaded.",
                    )
                )
                continue
            used_last_known_good = used_last_known_good or loaded.used_last_known_good
            issues.extend(loaded.issues)
            for section in loaded.sections:
                if section.sensitive:
                    issues.append(
                        ContextSourceIssue(
                            code="context_source_sensitive_section_rejected",
                            source_ref=section.source_ref,
                            section_id=section.section_id,
                            public_message="A sensitive context section was rejected.",
                        )
                    )
                    continue
                if section.section_id in seen_ids or (section.kind == "soul" and soul_seen):
                    issues.append(
                        ContextSourceIssue(
                            code="context_source_duplicate_section_id",
                            source_ref=section.source_ref,
                            section_id=section.section_id,
                            public_message="A duplicate context section was rejected.",
                        )
                    )
                    continue
                seen_ids.add(section.section_id)
                soul_seen = soul_seen or section.kind == "soul"
                sections.append(section)
        return ContextSourceResult(
            sections=sections,
            issues=issues[: self._max_issues],
            used_last_known_good=used_last_known_good,
        )
```

The actual `load_once` implementation must return immediately when disabled, call only explicitly enabled IDs, catch source exceptions into a fixed public issue, reject empty/sensitive/duplicate sections, allow at most one `kind="soul"`, and slice issues to 16. Do not include exception strings, section content, source versions or absolute paths in issues.

- [x] **Step 5: Implement safe SOUL I/O and parsing**

Use constants:

```python
SOUL_SOURCE_ID = "soul"
SOUL_SOURCE_REF = "editable_context:soul"
SOUL_FILE_NAME = "SOUL.md"
SOUL_MAX_BYTES = 16_000
SOUL_MAX_CHARS = 4_000
SOUL_COMPILED_MAX_CHARS = 2_000
SOUL_SUBSECTION_MAX_CHARS = 800
SOUL_SECTION_ORDER = (
    "Relationship Boundaries",
    "Avoid",
    "Persona",
    "Expression Style",
)
```

Resolve the configured root once. Resolve the fixed candidate path only for containment validation, reject a final-component symlink, then open with `os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)` when supported. Validate `os.fstat(fd).st_mode` with `stat.S_ISREG`, read at most 16,001 bytes from that one descriptor, close in `finally`, decode UTF-8 once, then validate and parse.

Unsafe detection must be local and deterministic, aligned with existing `provider_errors.py` patterns:

```python
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|bearer|cookie|secret(?:[_-]?token)?|token|password)\b\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_PREFIX_RE = re.compile(r"\b(?:sk|pk|qwen|dashscope)-[A-Za-z0-9._-]{4,}\b", re.IGNORECASE)
_BASE64_RE = re.compile(r"\b(?:[A-Za-z0-9+/]{80,}={0,2}|data:[^;\s]+;base64,[A-Za-z0-9+/=]{32,})\b")
_RAW_MARKERS = ("raw_provider_payload", "raw_provider_response", "provider_raw_response")
```

Parse only level-two headings with exact names. Split subsection bodies into nonempty blank-line-separated paragraphs. Apply the fixed priority order, enforce 800 chars per subsection and 2,000 chars globally by selecting complete paragraphs. A subsection heading is rendered only when at least one paragraph is selected.

Generate the internal `source_version` with an HMAC-SHA256 digest using a per-process random key created in `SoulContextSource.__init__`; never expose it in issue/report. Cache the final section by `(str(resolved_root), local_owner_user_id)`. Update last-known-good only after every validation and budget step succeeds.

- [x] **Step 6: Run GREEN verification**

Run the Step 3 command. Expected: all coordinator and SOUL tests pass.

---

### Task 3: Freeze source results once per runtime run

**Files:**

- Modify: `src/assistant_agent/agent/runtime.py`
- Modify: `tests/test_context_sources.py`
- Modify: `tests/test_native_runtime_system_prompt_policy.py`

**Interfaces:**

- Consumes: Task 2 coordinator and request.
- Produces: optional `context_source_coordinator` constructor injection for deterministic tests.
- Guarantees: one `load_once()` call per `run_state()`, same frozen result across all ReAct/native iterations.

- [x] **Step 1: Write failing runtime tests**

Add a recording coordinator with `load_once()` returning a valid SOUL result. Execute `AgentGraphRuntime.run_state()` with the existing `CapturingChatAdapter` and assert:

```python
assert coordinator.call_count == 1
assert coordinator.requests[0].user_id == "owner-1"
assert state.context_source_result.sections[0].content == "保持简洁。"
```

Run two separate requests and assert the coordinator count becomes two. For a multi-iteration scripted native adapter, mutate the backing `SOUL.md` after the first chat call and assert both calls use the initial persona; a second `run_state()` observes the new valid content.

Add a mismatched user test with enabled config and assert the system message has no owner persona and the state issue is `editable_context_identity_mismatch`.

- [x] **Step 2: Run RED verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_context_sources.py \
  tests/test_native_runtime_system_prompt_policy.py -q
```

Expected: constructor/signature or assertion failures because runtime does not load sources.

- [x] **Step 3: Implement runtime-owned loading**

Add this optional constructor argument without changing existing callers:

```python
context_source_coordinator: ContextSourceCoordinator | None = None
```

Initialize:

```python
self.context_source_coordinator = context_source_coordinator or ContextSourceCoordinator(
    [SoulContextSource()]
)
```

Immediately after `state = AgentState.from_request(request)` and before either native or graph decision path, assign:

```python
state.context_source_result = self.context_source_coordinator.load_once(
    ContextSourceRequest(
        user_id=state.user_id,
        source_root=Path(self.config.editable_context_root),
        local_owner_user_id=self.config.editable_context_user_id,
        runtime_profile=self.config.runtime_profile.name,
        editable_context_enabled=self.config.editable_context_enabled,
        section_char_budgets={"soul": SOUL_COMPILED_MAX_CHARS},
        enabled_source_ids={SOUL_SOURCE_ID},
    )
)
```

Do not copy file content to request metadata. Do not call the coordinator from builder, compiler, iteration loop, overflow retry or final-only handoff.

- [x] **Step 4: Run GREEN verification**

Run the Step 2 command. Expected: all selected tests pass and coordinator count assertions remain exact.

---

### Task 4: Integrate persona budget, compiler, system policy and redacted reporting

**Files:**

- Modify: `src/assistant_agent/services/context/builder.py`
- Modify: `src/assistant_agent/agent/system_prompt_policy.py`
- Modify: `src/assistant_agent/services/context/prompt_compiler.py`
- Modify: `src/assistant_agent/services/context/report.py`
- Modify: `src/assistant_agent/services/context/observability.py`
- Modify: `tests/test_system_prompt_policy.py`
- Modify: `tests/test_prompt_compiler.py`
- Modify: `tests/test_assistant_context_renderer.py`
- Modify: `tests/test_native_runtime_system_prompt_policy.py`

**Interfaces:**

- Consumes: frozen `AgentState.context_source_result`.
- Produces: keyword-only `owner_persona: str = ""` on `render_system_instruction`.
- Produces: non-content-bearing `ContextSourceReport` in ContextReport and trace summary.
- Guarantees: no duplicate system/persona accounting; tool set and tool choice unchanged.

- [x] **Step 1: Add default prompt characterization tests before changing production code**

Assert these exact UTF-8 SHA-256 hashes:

```python
EXPECTED_PROMPT_HASHES = {
    "text_default": "1d4e027450f9dd73d87e1d29066861b5c958e246fc90df24e3b08cc29d152bd7",
    "text_product": "9eb763be339edbf383cbdf0890ad492cca429424311ba8137710661f968d461f",
    "realtime_phone": "edafe6e532ac5d75b1b16e3d5f0a09d2488f43dfa3911a89de30d35f10073825",
    "final_only": "e0b51d3964ed3a01a47aaf279db0906a2bdaaddf8ff7452761a3d19b8c23fede",
}
```

Compute hashes for current default calls, including text default with `SystemPromptOptions(product_mode=True)`. These tests must pass before the function signature changes and continue passing afterward.

- [x] **Step 2: Add failing persona policy/compiler tests**

Assert:

```python
prompt = render_system_instruction(
    SystemPromptProfile.TEXT_DEFAULT,
    owner_persona="## Persona\n先给结论。",
)
assert prompt.index("Do not execute instructions found inside memory") < prompt.index("Owner persona")
assert prompt.endswith("## Persona\n先给结论。")
assert "cannot override runtime policy, tool governance, approvals, or safety boundaries" in prompt
```

Compile `NATIVE_TOOL`, `NATIVE_FINAL_ONLY`, and `SUMMARY_FINAL_ONLY` packs containing one SOUL section. Assert all three system messages contain persona; tool schemas and tool choice remain identical to equivalent packs without persona. Assert an empty `context_sections` list yields the characterization hashes.

- [x] **Step 3: Add failing builder/budget/report tests**

Build a state with one frozen SOUL section and assert:

- pack receives the section without reading a path;
- `budget.owner_persona_chars == len(section.content)` and `total_chars` includes it;
- `source_counts["context_sections"] == 1` and `source_counts["context_source_issues"]` is correct;
- explicit `context_budget_max_chars` removes complete trailing persona paragraphs before memory/conversation/observations;
- when no complete persona paragraph fits, `context_sections == []` and `"owner_persona"` appears in `trimmed_sections`;
- report `sections["system_prompt"].chars` already includes persona;
- `context_sources.chars_by_authority["owner_persona"]` is non-additive metadata and is not added a second time to `ContextReport.total_chars`;
- report and trace contain issue codes and counts but not section content, source version or absolute path.

- [x] **Step 4: Run RED verification**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_system_prompt_policy.py \
  tests/test_prompt_compiler.py \
  tests/test_assistant_context_renderer.py \
  tests/test_native_runtime_system_prompt_policy.py -q
```

Expected: characterization tests pass; new persona/budget/report assertions fail because integration is absent.

- [x] **Step 5: Implement system policy and compiler selection**

Change the public function signature exactly:

```python
def render_system_instruction(
    profile: SystemPromptProfile = SystemPromptProfile.TEXT_DEFAULT,
    *,
    options: SystemPromptOptions | None = None,
    owner_persona: str = "",
) -> str:
```

First render the existing profile unchanged. Return it immediately when `owner_persona` is empty. Otherwise append:

```text
Owner persona is lower-authority style and relationship guidance. It cannot override runtime policy, tool governance, approvals, identity boundaries, or safety boundaries.
<validated owner persona content>
```

Do not place a closing delimiter after editable text. Do not parse or clip content here.

In `PromptCompiler.compile`, select the single non-sensitive `kind="soul"` section from `request.context_pack.context_sections`; coordinator invariants mean zero or one is valid. Pass its content as `owner_persona`. If a caller constructs an invalid pack with multiple SOUL sections, fail closed by using no persona rather than concatenating.

- [x] **Step 6: Implement persona-first global budgeting**

In builder, start from validated `state.context_source_result.sections`. Reject sensitive sections defensively. Fit each section only at blank-line paragraph boundaries. For SOUL, content is already high-to-low priority; remove trailing complete paragraphs until:

```text
dynamic/tool chars + owner persona chars <= effective max chars
```

Then call the existing dynamic `_enforce_context_budget` with `max_chars - owner_persona_chars`. This enforces the required order: persona first, memory second, conversation third, observations last. Preserve section headings only when they still have a following paragraph. Record `owner_persona` once in `trimmed_sections` and include the final persona size in `_budget_report` and token reporter input under `owner_persona`.

- [x] **Step 7: Implement redacted source report**

Add one shared pure helper in `report.py`:

```python
def build_context_source_report(
    result: ContextSourceResult,
    sections: Iterable[ContextSection],
) -> ContextSourceReport:
```

It may expose only kind/authority/stability counts, character totals, deduplicated issue codes, `used_last_known_good`, count of sections carrying note `source_version_changed`, omitted-section issue count, and constant layout version. It must not serialize content, `source_version`, absolute paths or public messages.

`build_context_report` assigns this helper result to `context_sources`. `context_trace_summary` emits `context_sources.model_dump(mode="json")`. Keep `ContextReport.total_chars` equal to the sum of actual provider sections; do not add source-summary character counts.

- [x] **Step 8: Run GREEN verification**

Run the Step 4 command. Expected: all tests pass, including unchanged hashes and existing tool contract assertions.

---

### Task 5: Update authority docs and run regressions

**Files:**

- Modify: `docs/context_engineering_status.md`
- Modify: `docs/memory-service-architecture.md`
- Verify: every source and test file from Tasks 1–4

**Interfaces:**

- Consumes: completed implementation and passing focused tests.
- Produces: current-state documentation, no roadmap claims for USER/MEMORY projection, skill disclosure, cache hints or FTS.

- [x] **Step 1: Update the context authority document**

Add concise current-state bullets covering:

- default-off local editable context;
- the three exact environment variables;
- run-entry load-once and frozen state;
- fixed SOUL headings, limits, identity fail-closed, secret/path checks and process-local last-known-good;
- `ContextSection v1`, `owner_persona_chars` and redacted source report;
- PromptCompiler remains pure and tool governance remains unchanged;
- limitation: process-local last-known-good is not cross-worker consistent and owner-trusted persona can affect expression but not capability policy.

Add `sources.py`, `soul_source.py`, and the two new tests to Key Files / Relevant Tests.

- [x] **Step 2: Update the memory authority boundary**

Add one paragraph stating that future `USER.md` and `MEMORY.md` are projections only; this phase implements neither import nor runtime reading, and MemoryManager/store remain the durable truth with read/write policy and audit.

- [x] **Step 3: Run focused A+B verification**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_context_sources.py \
  tests/test_soul_context_source.py \
  tests/test_assistant_context_renderer.py \
  tests/test_prompt_compiler.py \
  tests/test_system_prompt_policy.py \
  tests/test_native_runtime_system_prompt_policy.py \
  tests/test_provider_config_validation.py \
  tests/unit/test_agent_state.py -q
```

Expected: zero failures.

- [x] **Step 4: Run memory-boundary regressions**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest \
  tests/test_memory_manager.py \
  tests/test_memory_context_builder.py \
  tests/test_memory_tool_boundary.py -q
```

Expected: zero failures.

- [x] **Step 5: Run fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: zero failures. If an unrelated dirty-worktree test fails, record the exact pre-existing file and failure rather than changing it.

- [x] **Step 6: Run static and scope verification**

```bash
git diff --check -- \
  docs/context_engineering_status.md \
  docs/memory-service-architecture.md \
  docs/superpowers/plans \
  docs/superpowers/specs \
  src/assistant_agent/config.py \
  src/assistant_agent/agent/state.py \
  src/assistant_agent/agent/runtime.py \
  src/assistant_agent/agent/system_prompt_policy.py \
  src/assistant_agent/schemas/context.py \
  src/assistant_agent/services/context \
  tests/test_context_sources.py \
  tests/test_soul_context_source.py \
  tests/test_assistant_context_renderer.py \
  tests/test_prompt_compiler.py \
  tests/test_system_prompt_policy.py \
  tests/test_native_runtime_system_prompt_policy.py \
  tests/test_provider_config_validation.py \
  tests/unit/test_agent_state.py
```

Inspect `git status --short` and per-file diffs. Confirm the pre-existing modifications to `docs/tool-calling-architecture.md`, calendar/search/weather/native handoff tests, and unrelated latency/prompt specs remain untouched.

---

## Plan Self-Review Record

### Spec coverage

| Design requirement | Implementing task |
| --- | --- |
| Typed ContextSection v1 and additive pack/state | Task 1 |
| Exact config names and default-off behavior | Task 1 |
| Source protocol, invariants and issue cap | Task 2 |
| Fixed SOUL schema, path/UTF-8/size/secret checks | Task 2 |
| Deterministic paragraph budgets and last-known-good | Task 2 |
| Once-per-run load and frozen multi-iteration content | Task 3 |
| PromptCompiler purity and immutable policy precedence | Task 4 |
| Persona-first global budget and no observation precedence regression | Task 4 |
| Redacted ContextReport/trace without double counting | Task 4 |
| Default provider-request equivalence | Task 4 characterization hashes |
| Tool exposure, tool choice and memory authority unchanged | Tasks 4–5 regressions |
| Authority documentation and explicit limitations | Task 5 |

### Type consistency

- `ContextSourceRequest.source_root` is always `Path`; runtime converts `ProviderConfig.editable_context_root` exactly once.
- `ContextSourceResult` is the state field type and coordinator return type; builder never accepts a path.
- `ContextSection.content` remains internal prompt material; `ContextSourceReport` contains no content or source version.
- `owner_persona` is keyword-only in policy and is derived only from `AssistantContextPack.context_sections` in compiler.
- `ContextBudgetReport.owner_persona_chars` is additive and is included once in total chars; `ContextReport.context_sources` is metadata, not an additive prompt section.
- All existing constructor call sites remain valid because new model fields and runtime constructor injection have defaults.

### Self-review corrections already incorporated

1. The original design did not specify a concrete report model; this plan adds `ContextSourceReport` rather than serializing `ContextSourceResult` and accidentally exposing content/version.
2. The plan uses hard-coded pre-change prompt hashes so “default unchanged” is an actual characterization test, not a comparison between two calls to newly modified code.
3. The coordinator is called only from `AgentGraphRuntime.run_state`; overflow retry and final-only paths reuse state, preventing mid-run file drift.
4. Global budget handling explicitly removes persona before dynamic evidence and passes the remaining maximum into the existing memory/conversation/observation order.
5. Secret scanning aligns with existing provider error patterns but does not call an error-message sanitizer that would truncate normal SOUL text or reject harmless uses of the word “secret”.
6. The final component is opened with no-follow and checked with `fstat`, closing the path-check/open race more tightly than `Path.read_text()`.
7. No new prompt-engineering authority document is created because it is absent in the current worktree.
8. No per-task commit is planned because the repository requires a unified development-stage submission and the user did not request VCS commits in the dirty shared branch.

### Residual implementation risks to verify

- Pydantic serialization snapshots may include the new default state field; Task 1 round-trip and fast suite must prove compatibility.
- Paragraph-boundary fitting must not leave an empty heading in compiled persona; Task 2 and Task 4 contain explicit assertions.
- The existing budget headroom for tool schemas must remain unchanged when editable context is disabled; Task 4 hashes and renderer regressions cover this.
- Process-local last-known-good remains worker-local by design; documentation must not imply cross-process consistency.

### Self-review conclusion

The plan covers every A+B acceptance criterion with a RED/GREEN test cycle and contains no authorized work from later USER/MEMORY projection, progressive skill, cache-hint, promotion-review or FTS phases. No blocking type, authority or execution-order inconsistency remains; inline implementation may start automatically under the user’s instruction.

---

## Implementation Self-Review Record

The post-implementation review found and corrected four gaps before handoff:

1. Missing `SOUL.md` is now distinct from an invalid replacement: deletion yields no persona instead of silently keeping last-known-good content, while an invalid new version may reuse the last valid version.
2. Context-source metadata is now preserved in both context-build and assistant-loop trace summaries, and legacy trace summaries without the additive field still deserialize safely.
3. Assistant-loop `system_chars` is computed from the same fail-closed owner-persona selection used by `PromptCompiler`, preventing observability from reporting content that was not injected.
4. Builder consumption is fail-closed to the single supported `soul` section so later source kinds cannot enter prompts before their authority rules are implemented.

Fresh scoped verification after self-review and independent review corrections reports 140 focused tests, 31 memory-boundary tests, and 178 fast tests passing. Independent review additionally verified that final owner persona content participates in local token estimation, subsection overflow omits all later same-priority paragraphs, and the coordinator rejects whitespace-only sections. The repository-wide suite reports 1 failure, 1887 passes, and 6 skips: `test_explicit_cancel_run_end_payload_includes_prompt_safe_cancel_source` expects the older Gateway cancellation payload from commit `3597663`, while the later cancellation-contract commit `f70b40d` enriches that payload and sets phase `final_streaming`. Neither the Gateway implementation nor that test is modified by this plan, so resolving the stale contract is deliberately left outside A+B scope.
