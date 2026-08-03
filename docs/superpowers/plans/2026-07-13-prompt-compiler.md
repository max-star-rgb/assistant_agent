# PromptCompiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route all production native and final-only `ChatRequest` construction through one deterministic `PromptCompiler` without changing model-visible behavior.

**Architecture:** Add a stateless compiler under `services/context` with explicit native-tool, native-final-only, and summary-final-only modes. Existing assistant-loop and runtime helpers become compatibility adapters; context building, profile trust, provider calls, retry, memory, tools, and trace side effects stay outside.

**Tech Stack:** Python 3.11+, dataclasses, `StrEnum`, Pydantic `ChatRequest`, pytest, `AssistantContextPack`, provider-native `ToolSpec` adapters.

## Global Constraints

- Use `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` for Python and pytest.
- Do not install dependencies or call a real external Provider.
- Preserve all prompt text, message order, tool schemas, tool choices, fallback IDs, temperature `0.2`, and max tokens `1024`.
- Keep legacy `render_prompt_json_context` unchanged.
- Keep memory, compaction, tool selection, overflow retry, and trace ownership outside the compiler.
- Use `apply_patch` for manual edits.
- Commit design, plan, code, tests, and authority-document update together only after verification.

---

### Task 1: Add PromptCompiler with TDD coverage

**Files:**
- Create: `src/assistant_agent/services/context/prompt_compiler.py`
- Create: `tests/test_prompt_compiler.py`

**Interfaces:**
- Consumes: `SystemPromptProfile`, `SystemPromptOptions`, `AssistantContextPack`, `ToolSpec`, `ChatRequest`, `ChatStreamCallback`.
- Produces: `PromptCompileMode`, `PromptCompileRequest`, `PromptCompileResult`, `PromptCompiler.compile`, `prompt_tool_specs_for_mode`.

- [ ] **Step 1: Write failing tests for the three modes**

Create `tests/test_prompt_compiler.py` with a minimal pack fixture and these exact assertions:

```python
from copy import deepcopy

from assistant_agent.agent.system_prompt_policy import SystemPromptOptions, SystemPromptProfile, render_system_instruction
from assistant_agent.schemas.context import AssistantContextPack
from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import ToolSpec
from assistant_agent.services.context.prompt_compiler import PromptCompileMode, PromptCompileRequest, PromptCompiler
from assistant_agent.services.context.renderer import render_final_only_context, render_native_tool_context


def _pack(text: str = "帮我查耳机") -> AssistantContextPack:
    product = ToolSpec(name="product_search", required_inputs=["query"])
    hidden = ToolSpec(name="render_3d", required_inputs=["scene_description"])
    return AssistantContextPack(
        request=UserRequest(user_id="u1", session_id="s1", text=text),
        observations=[{"tool_name": "product_search", "status": "succeeded"}],
        tool_specs=[product, hidden],
        prompt_tool_specs=[product],
        iteration=1,
        max_iterations=5,
    )


def _compile(pack: AssistantContextPack, mode: PromptCompileMode, **overrides):
    values = {
        "user_id": "u1",
        "session_id": "s1",
        "mode": mode,
        "user_query_fallback": "fallback",
        "profile": SystemPromptProfile.TEXT_DEFAULT,
        "options": SystemPromptOptions(product_mode=True),
        "context_pack": pack,
        "observations": tuple(pack.observations),
        "native_calls": ({},),
        "tool_call_id_prefix": "call_",
    }
    values.update(overrides)
    return PromptCompiler().compile(PromptCompileRequest(**values))


def test_native_tool_mode_preserves_provider_request_contract() -> None:
    pack = _pack()
    callback = lambda _text, _payload: None
    result = _compile(pack, PromptCompileMode.NATIVE_TOOL, stream_callback=callback)
    request = result.chat_request
    assert request.messages[0]["content"] == render_system_instruction(
        SystemPromptProfile.TEXT_DEFAULT,
        options=SystemPromptOptions(product_mode=True),
    )
    assert request.messages[1]["content"] == render_native_tool_context(pack).native_user_message
    assert request.messages[2]["tool_calls"][0]["id"] == "call_1"
    assert request.messages[3]["tool_call_id"] == "call_1"
    assert [tool["function"]["name"] for tool in request.tools] == ["product_search"]
    assert request.tool_choice == "auto"
    assert request.temperature == 0.2
    assert request.max_tokens == 1024
    assert request.stream_callback is callback


def test_native_final_only_keeps_tool_evidence_but_disables_tools() -> None:
    pack = _pack()
    result = _compile(
        pack,
        PromptCompileMode.NATIVE_FINAL_ONLY,
        profile=SystemPromptProfile.FINAL_ONLY,
        options=SystemPromptOptions(),
        user_query_fallback="native runtime final answer",
        tool_call_id_prefix="native_runtime_call_",
    )
    assert result.chat_request.messages[1]["content"] == render_native_tool_context(pack).native_user_message
    assert any(message["role"] == "tool" for message in result.chat_request.messages)
    assert result.chat_request.tools == []
    assert result.chat_request.tool_choice == "none"


def test_summary_final_only_uses_summary_prompt_as_query_and_user_message() -> None:
    pack = _pack()
    expected = render_final_only_context(pack).final_only_prompt
    result = _compile(
        pack,
        PromptCompileMode.SUMMARY_FINAL_ONLY,
        profile=SystemPromptProfile.FINAL_ONLY,
        options=SystemPromptOptions(),
        native_calls=(),
    )
    assert result.chat_request.user_query == expected
    assert result.chat_request.messages == [
        {"role": "system", "content": render_system_instruction(SystemPromptProfile.FINAL_ONLY)},
        {"role": "user", "content": expected},
    ]
    assert result.chat_request.tools == []
    assert result.chat_request.tool_choice is None


def test_compile_preserves_raw_call_and_does_not_mutate_inputs() -> None:
    pack = _pack()
    calls = ({"raw": {"id": "raw-1", "type": "function", "function": {"name": "product_search", "arguments": "{\\"query\\":\\"耳机\\"}"}}},)
    before_pack = pack.model_dump(mode="json")
    before_calls = deepcopy(calls)
    result = _compile(pack, PromptCompileMode.NATIVE_TOOL, native_calls=calls)
    assert result.chat_request.messages[2]["tool_calls"][0] == calls[0]["raw"]
    assert pack.model_dump(mode="json") == before_pack
    assert calls == before_calls
```

- [ ] **Step 2: Verify the new tests fail before implementation**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_prompt_compiler.py
```

Expected: collection fails with `ModuleNotFoundError` for `assistant_agent.services.context.prompt_compiler`.

- [ ] **Step 3: Implement the compiler contracts and entrypoint**

Create `prompt_compiler.py` with:

```python
class PromptCompileMode(StrEnum):
    NATIVE_TOOL = "native_tool"
    NATIVE_FINAL_ONLY = "native_final_only"
    SUMMARY_FINAL_ONLY = "summary_final_only"


@dataclass(frozen=True)
class PromptCompileRequest:
    user_id: str
    session_id: str
    mode: PromptCompileMode
    user_query_fallback: str
    profile: SystemPromptProfile
    options: SystemPromptOptions
    context_pack: AssistantContextPack
    observations: tuple[dict[str, Any], ...]
    native_calls: tuple[dict[str, Any], ...]
    tool_call_id_prefix: str
    stream_callback: ChatStreamCallback | None = None
    temperature: float = 0.2
    max_tokens: int = 1024


@dataclass(frozen=True)
class PromptCompileResult:
    chat_request: ChatRequest
    system_instruction: str
    rendered_context: RenderedAssistantContext
    selected_tool_specs: tuple[ToolSpec, ...]
```

Implement `PromptCompiler.compile` as this deterministic pipeline:

```python
system = render_system_instruction(request.profile, options=request.options)
rendered = (
    render_final_only_context(request.context_pack)
    if request.mode == PromptCompileMode.SUMMARY_FINAL_ONLY
    else render_native_tool_context(request.context_pack)
)
user_content = (
    rendered.final_only_prompt
    if request.mode == PromptCompileMode.SUMMARY_FINAL_ONLY
    else rendered.native_user_message
) or ""
messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
if request.mode != PromptCompileMode.SUMMARY_FINAL_ONLY:
    messages.extend(_native_tool_messages(request))
selected = prompt_tool_specs_for_mode(request.context_pack, request.mode)
user_query = user_content if request.mode == PromptCompileMode.SUMMARY_FINAL_ONLY else (request.context_pack.request.text or request.user_query_fallback)
tool_choice = "auto" if request.mode == PromptCompileMode.NATIVE_TOOL else ("none" if request.mode == PromptCompileMode.NATIVE_FINAL_ONLY else None)
```

Construct `ChatRequest` from those values. Move the existing raw payload logic verbatim into `_native_tool_call_payload`; generate missing IDs with `f"{request.tool_call_id_prefix}{index + 1}"`; serialize arguments and observations with `ensure_ascii=False`. `prompt_tool_specs_for_mode` returns `()` for final-only modes and `tuple(pack.prompt_tool_specs or pack.tool_specs)` for native tools.

- [ ] **Step 4: Run compiler tests to green**

Run the Task 1 test command. Expected: all tests pass.

---

### Task 2: Migrate assistant-loop builders

**Files:**
- Modify: `src/assistant_agent/agent/assistant_loop_nodes.py:28-58,471-551,805-842,1722-1731`
- Modify: `tests/test_native_runtime_system_prompt_policy.py:140-272`

**Interfaces:**
- Consumes: Task 1 compiler contracts.
- Produces: existing assistant-loop private helper signatures backed by the compiler.

- [ ] **Step 1: Add characterization assertions before changing production code**

Add a test with empty user text and one observation asserting:

```python
chat_request = _build_native_tool_chat_request(context, state)
assert chat_request.user_query == "native_tools assistant turn"
assert chat_request.messages[2]["tool_calls"][0]["id"] == "call_1"
assert chat_request.tool_choice == "auto"
assert chat_request.temperature == 0.2
assert chat_request.max_tokens == 1024
```

Extend the existing assistant-loop final-only test:

```python
final_request = adapter.requests[0]
assert final_request.user_query == final_request.messages[1]["content"]
assert final_request.tools == []
assert final_request.tool_choice is None
assert all(message["role"] != "tool" for message in final_request.messages)
```

- [ ] **Step 2: Run characterization tests against the old builders**

Run:

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_native_runtime_system_prompt_policy.py
```

Expected: all tests pass.

- [ ] **Step 3: Replace assistant-loop request assembly**

- `_build_native_tool_chat_request` compiles `NATIVE_TOOL` with `TEXT_DEFAULT`, `SystemPromptOptions(product_mode=True)`, fallback `"native_tools assistant turn"`, prefix `"call_"`, current observations, and metadata native calls.
- `_build_native_tool_messages` returns `_build_native_tool_chat_request(...).messages`.
- `_selected_native_tool_specs` delegates to `prompt_tool_specs_for_mode` for report compatibility.
- `_request_final_answer_after_tool_limit` compiles `SUMMARY_FINAL_ONLY` with `FINAL_ONLY`, default options, no native calls, and passes `.chat_request` to the adapter.
- Remove local tool-call payload/arguments helpers and now-unused imports.

The summary-final call must use:

```python
compiled = PromptCompiler().compile(
    PromptCompileRequest(
        user_id=state.user_id,
        session_id=state.session_id,
        mode=PromptCompileMode.SUMMARY_FINAL_ONLY,
        user_query_fallback="unused",
        profile=SystemPromptProfile.FINAL_ONLY,
        options=SystemPromptOptions(),
        context_pack=context_pack,
        observations=tuple(observations),
        native_calls=(),
        tool_call_id_prefix="call_",
    )
)
result = chat_adapter.chat(compiled.chat_request)
```

- [ ] **Step 4: Run assistant-loop and compiler tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_prompt_compiler.py tests/test_native_runtime_system_prompt_policy.py tests/test_assistant_context_renderer.py
```

Expected: all tests pass.

---

### Task 3: Migrate AgentGraphRuntime builders and context report inputs

**Files:**
- Modify: `src/assistant_agent/agent/runtime.py:1-54,868-945,1006-1098,1622-1640,1887-1917`
- Modify: `tests/test_native_runtime_system_prompt_policy.py:27-138`
- Modify: `tests/test_native_tool_call_handoff.py:1210-1340`

**Interfaces:**
- Consumes: Task 1 compiler contracts.
- Produces: existing runtime request-builder signatures returning compiler-built `ChatRequest` values.

- [ ] **Step 1: Strengthen runtime characterization tests**

For normal runtime requests assert original user query, temperature `0.2`, and max tokens `1024`. For native final-only retain exact assertions that tools are empty, tool choice is `"none"`, final-only system policy is used, and a tool evidence message remains present.

- [ ] **Step 2: Run runtime characterization tests before refactor**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_native_runtime_system_prompt_policy.py tests/test_native_tool_call_handoff.py
```

Expected: all tests pass.

- [ ] **Step 3: Replace runtime request assembly**

- `_native_runtime_chat_request` chooses `NATIVE_FINAL_ONLY` only for the `FINAL_ONLY` profile, otherwise `NATIVE_TOOL`; compile with current request options, fallback `"native runtime assistant turn"`, prefix `"native_runtime_call_"`, observations, native calls, and stream callback.
- `_native_runtime_final_only_chat_request` compiles `NATIVE_FINAL_ONLY` with fallback `"native runtime final answer"` and the same evidence messages.
- `_record_native_runtime_context_report` accepts `PromptCompileResult` and uses `compilation.system_instruction` plus `list(compilation.selected_tool_specs)`.
- Delete runtime-local selected-tool and tool-call payload helpers; remove now-unused `json`, schema adapter, and renderer imports.

The report call becomes:

```python
report = build_context_report(
    context_pack,
    system_prompt=compilation.system_instruction,
    selected_tool_specs=list(compilation.selected_tool_specs),
).model_dump(mode="json")
```

- [ ] **Step 4: Run runtime/compiler tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q tests/test_prompt_compiler.py tests/test_native_runtime_system_prompt_policy.py tests/test_native_tool_call_handoff.py
```

Expected: all tests pass.

---

### Task 4: Document and verify the completed phase

**Files:**
- Modify: `docs/context_engineering_status.md:10-35,105-120,140-155`
- Modify: `docs/superpowers/specs/2026-07-13-prompt-compiler-design.md`
- Modify: `docs/superpowers/plans/2026-07-13-prompt-compiler.md`

**Interfaces:**
- Consumes: migrated compiler and call sites.
- Produces: authority documentation and final verification evidence.

- [ ] **Step 1: Update the authority document**

Add this implemented-state bullet without claiming later Markdown/token work:

```markdown
- 生产 provider-native `ChatRequest` 现在统一通过无副作用 `PromptCompiler` 编译；native tool、native-context final-only 和 summary final-only 使用显式 mode，保留各自既有 renderer、tool choice、tool-call evidence 和生成参数。legacy prompt-json renderer 仍只用于离线兼容与测试。
```

- [ ] **Step 2: Run focused tests**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/test_prompt_compiler.py \
  tests/test_system_prompt_policy.py \
  tests/test_native_runtime_system_prompt_policy.py \
  tests/test_assistant_context_renderer.py \
  tests/test_phase3_skill_system_gate.py \
  tests/test_native_tool_call_handoff.py
```

Expected: all selected tests pass.

- [ ] **Step 3: Run the repository fast suite**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -m fast -q
```

Expected: zero failures.

- [ ] **Step 4: Verify convergence and diff hygiene**

```bash
rg -n '"role": "system"|"tool_calls": \[tool_call_payload\]' \
  src/assistant_agent/agent/assistant_loop_nodes.py \
  src/assistant_agent/agent/runtime.py
git diff --check -- AGENTS.md docs src tests .codex/skills
git status --short
```

Expected: no migrated function manually assembles a complete native request; diff check exits zero; status contains only this phase's files.

- [ ] **Step 5: Commit the verified phase once**

```bash
git add src/assistant_agent/services/context/prompt_compiler.py \
  src/assistant_agent/agent/assistant_loop_nodes.py \
  src/assistant_agent/agent/runtime.py \
  tests/test_prompt_compiler.py \
  tests/test_native_runtime_system_prompt_policy.py \
  tests/test_native_tool_call_handoff.py \
  docs/context_engineering_status.md \
  docs/superpowers/specs/2026-07-13-prompt-compiler-design.md \
  docs/superpowers/plans/2026-07-13-prompt-compiler.md
git commit -m "Unify production prompt compilation"
```

Expected: one cohesive commit and no unrelated staged files.
