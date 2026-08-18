# 内建 Tool 官方原生化迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将全部内建 Tool 从项目自定义 `ToolBase` 类迁移为 LangChain 官方 `@tool` 工厂，删除双 schema 和旧 runtime binding，同时保持治理、安全和标准 `ToolMessage` 行为。

**Architecture:** 每个 Plugin 调用具名 `create_*_tool(...) -> BaseTool` 工厂；工厂闭包持有 adapter/service，内部官方 `@tool` 函数只声明模型可见参数和 `ToolRuntime`。普通 runtime/output helper 负责可信上下文与 `ToolResult` 投影，但不创建或修改 Tool schema。

**Tech Stack:** Python 3.12、LangChain `@tool`/`BaseTool`、LangGraph `ToolRuntime`/`ToolNode`、Pydantic v2、pytest、Ruff。

**Spec:** `docs/superpowers/specs/2026-08-18-native-tool-definition-migration-design.md`

## Global Constraints

- 默认使用 `/home/lenovo1/miniconda3/envs/hello_agent/bin/python` 和 `MULTIMODAL_AGENT_PROVIDER_MODE=mock`。
- 全部 pytest、TDD 和静态检查保持 local/offline，不调用真实 Provider。
- 不保留旧 `*Tool` 类兼容别名，不引入新的动态 schema 生成器或项目私有 Tool 基类。
- 模型不可控制的默认参数和 runtime-owned 参数不得出现在 `tool_call_schema`。
- 身份只来自 `ToolRuntime.server_info.user.identity`；写/生成 Tool 的 `effect` 与 HITL 语义保持。
- 保留领域 Request/Result、adapter/service 与 `ToolResult`，仅重构 Tool 定义和运行时边界。
- 不覆盖当前工作区中的用户改动；每次提交只暂存本任务文件。若在共享脏工作区执行，则将提交步骤改为只做 diff checkpoint，最终由用户决定提交。

---

### Task 1: 建立无 schema 职责的原生 Runtime 与输出边界

**Files:**
- Create: `src/assistant_agent/tools/runtime.py`
- Create: `src/assistant_agent/tools/native_boundary.py`
- Modify: `tests/core/contract/test_tool_contract.py`
- Modify: `tests/core/support.py`
- Create: `tests/tdd/native-builtin-tools/test_native_boundary.py`

**Interfaces:**
- Produces: `ToolContext`、`latest_human_request(state)`、`tool_context(runtime, *, metadata=None)`、`skill_reference_grants(state)`。
- Produces: `native_tool_response(tool_name, result)`、`invoke_native_tool(tool_name, operation)`、`builtin_tool_metadata(effect, *, availability=None)`、`native_idempotency_key(runtime)`。
- `invoke_native_tool` 返回 `tuple[list[dict[str, Any]], dict[str, Any]]`，业务失败抛 `ToolException`，未知异常先脱敏。
- Later tasks must import these functions; they must not import `ToolBase` or create Pydantic models dynamically.

- [ ] **Step 1: 写 runtime/output 边界的失败测试**

```python
def test_native_boundary_returns_standard_content_and_artifact() -> None:
    result = ToolResult(
        tool_name="probe",
        success=True,
        data={"value": "artifact-sentinel"},
        model_observation={"status": "ok"},
    )
    content, artifact = native_tool_response("probe", result)
    assert json.loads(content[0]["text"]) == {"status": "ok"}
    assert artifact == {"value": "artifact-sentinel"}


def test_native_boundary_rejects_failed_result() -> None:
    with pytest.raises(ToolException, match="failed-sentinel"):
        native_tool_response(
            "probe",
            ToolResult(tool_name="probe", success=False, error="failed-sentinel"),
        )


def test_native_idempotency_key_is_call_scoped(runtime) -> None:
    assert native_idempotency_key(runtime) == "native:thread-sentinel:call-sentinel"
```

- [ ] **Step 2: 运行 RED**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools/test_native_boundary.py
```

Expected: FAIL because `assistant_agent.tools.runtime` and `native_boundary` do not exist.

- [ ] **Step 3: 实现普通边界函数**

```python
# src/assistant_agent/tools/native_boundary.py
def native_tool_response(
    tool_name: str,
    result: ToolResult,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not result.success:
        raise ToolException(result.error or f"{tool_name} failed")
    observation = result.model_observation or result.data or {"status": "succeeded"}
    return (
        [{"type": "text", "text": json.dumps(
            observation, ensure_ascii=False, sort_keys=True, indent=2
        )}],
        dict(result.data or {}),
    )


def invoke_native_tool(tool_name: str, operation: Callable[[], ToolResult]):
    try:
        return native_tool_response(tool_name, operation())
    except ToolException:
        raise
    except Exception as exc:
        raise ToolException(sanitize_error_message(exc)) from exc


def builtin_tool_metadata(effect: ToolCategory, *, availability: str | None = None):
    metadata = {"effect": effect, "source": "builtin"}
    if availability is not None:
        metadata["availability"] = availability
    return metadata
```

Move `ToolContext` and the current `_latest_human_request`/`_tool_context`/`_skill_reference_grants` behavior from `tools/base.py` into `tools/runtime.py`. Implement:

```python
def native_idempotency_key(runtime: ToolRuntime[AssistantRunContext]) -> str:
    thread_id = runtime.execution_info.thread_id or "thread"
    return f"native:{thread_id}:{runtime.tool_call_id or 'tool-call'}"
```

- [ ] **Step 4: 将 core probe 改为官方 `@tool`**

Replace `_ProbeTool(ToolBase)` with:

```python
def _create_probe_tool() -> BaseTool:
    @tool("probe", response_format="content_and_artifact")
    def probe(
        value: str,
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[list[dict[str, str]], dict[str, str]]:
        user_id = authenticated_user_identity(runtime)
        return (
            [{"type": "text", "text": json.dumps({"status": "ok"})}],
            {"value": value, "user_id": user_id},
        )
    probe.metadata = builtin_tool_metadata("read")
    return probe
```

Update assertions to verify `tool.tool_call_schema.model_fields == {"value"}` and standard `ToolMessage(content, artifact)`. Convert `tests/core/support.py::ProbeTool` to a `create_probe_tool()` factory and update its core consumers.

- [ ] **Step 5: 运行 GREEN 与 core contract**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools/test_native_boundary.py \
  tests/core/contract/test_tool_contract.py
```

Expected: PASS.

- [ ] **Step 6: 创建任务 checkpoint**

Review only Task 1 files with `git diff --check` and `git diff -- <paths>`. In an isolated worktree, commit:

```bash
git add src/assistant_agent/tools/runtime.py \
  src/assistant_agent/tools/native_boundary.py \
  tests/core/contract/test_tool_contract.py tests/core/support.py
git commit -m "refactor: add native tool runtime boundary"
```

Do not force-add ignored `tests/tdd/**`.

---

### Task 2: 迁移文件、邮件与 Web 只读 Tool

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/local_file_access/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/email_access/tools.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/search_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/fetch_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/local_file_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/email_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/plugin.py`
- Create: `tests/tdd/native-builtin-tools/test_read_tool_factories.py`

**Interfaces:**
- Produces: `create_local_file_read_tool(root, *, max_file_bytes=...)`.
- Produces: `create_email_search_tool(backend: EmailBackend)` and `create_email_read_tool(backend: EmailBackend)`.
- Produces: `create_web_search_tool(adapter=None)` and `create_web_fetch_tool(adapter=None)`.
- Public schema fields must be:
  - `file_read`: `path`, `cursor`; hide `max_chars`.
  - `email_search`: `query`, `page_token`; hide `limit`.
  - `email_read`: `message_ids`; hide `max_total_chars`.
  - `web_search`: existing model-owned query fields except `limit`.
  - `web_fetch`: existing model-owned URL fields except `max_chars`, `content_format`.

- [ ] **Step 1: 写五个工厂的 schema 与真实调用失败测试**

```python
def test_read_tool_factories_are_native(tmp_path: Path) -> None:
    email_backend = MockEmailBackend()
    cases = [
        (create_local_file_read_tool(root=tmp_path), "file_read", {"max_chars"}),
        (create_email_search_tool(email_backend), "email_search", {"limit"}),
        (create_email_read_tool(email_backend), "email_read", {"max_total_chars"}),
        (create_web_search_tool(), "web_search", {"limit"}),
        (create_web_fetch_tool(), "web_fetch", {"max_chars", "content_format"}),
    ]
    for tool, name, hidden in cases:
        assert isinstance(tool, BaseTool)
        assert tool.name == name
        assert tool.metadata == {"effect": "read", "source": "builtin"}
        assert hidden.isdisjoint(tool.tool_call_schema.model_fields)
```

Add a real `ToolNode` file-read test that reads a temporary UTF-8 file and asserts standard artifact, plus a traversal attempt that returns a Tool error.

- [ ] **Step 2: 运行 RED**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools/test_read_tool_factories.py
```

Expected: FAIL because the `create_*_tool` exports do not exist.

- [ ] **Step 3: 用官方 `@tool` 工厂替换类**

Each factory follows this concrete shape:

```python
def create_email_search_tool(backend: EmailBackend) -> BaseTool:

    @tool(EMAIL_SEARCH_TOOL_NAME, response_format="content_and_artifact")
    def email_search(
        query: Annotated[
            str,
            Field(min_length=1, max_length=1_000, description="邮件查询条件。"),
        ],
        runtime: ToolRuntime[AssistantRunContext],
        page_token: Annotated[
            str | None,
            Field(max_length=2_000, description="上一页返回的 next_page_token。"),
        ] = None,
    ):
        request = EmailSearchRequest(query=query, page_token=page_token)
        return invoke_native_tool(
            EMAIL_SEARCH_TOOL_NAME,
            lambda: _execute_email_search(backend, request, tool_context(runtime)),
        )

    email_search.metadata = builtin_tool_metadata("read")
    return email_search
```

Move each old `_execute` body to a module-private `_execute_*` function accepting its adapter, validated Request and `ToolContext`. Local-file validation stays inside `_execute_local_file_read`; do not add a generic validation hook.

- [ ] **Step 4: 修改 Plugin 构造入口并运行 GREEN**

Replace `LocalFileReadTool`、`EmailSearchTool`、`EmailReadTool`、`WebSearchTool`、
`WebFetchTool` constructors with the five named factories from this task. Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools/test_read_tool_factories.py
```

Expected: PASS.

- [ ] **Step 5: 创建任务 checkpoint**

Run Ruff on modified modules and commit only Task 2 production files in an isolated worktree:

```bash
git commit -m "refactor: migrate read tools to native factories"
```

---

### Task 3: 迁移日历、联系人与住宿 Tool

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/calendar_weather_contacts/tools.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/calendar_weather_contacts/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/__init__.py`
- Update: `tests/tdd/lodging-search-date-compatibility/test_lodging_date_compatibility.py`
- Create: `tests/tdd/native-builtin-tools/test_personal_travel_tools.py`

**Interfaces:**
- Produces: `create_calendar_search_tool(adapter=None)`, `create_calendar_create_tool(adapter=None)`, `create_contacts_search_tool(adapter=None)`.
- Produces: `create_lodging_search_tool(adapter=None)`.
- `calendar_create` schema excludes `idempotency_key`; its Request receives `native_idempotency_key(runtime)`.
- `lodging_search` schema excludes `limit` and accepts ISO `YYYY-MM-DD` through a real `ToolNode` call.

- [ ] **Step 1: 写日期、隐藏参数和幂等注入 RED 测试**

```python
def test_lodging_native_tool_accepts_json_date_strings() -> None:
    message = invoke_with_tool_node(
        create_lodging_search_tool(),
        {"destination": "上海", "check_in": "2026-08-20", "check_out": "2026-08-22"},
    )
    assert message.status == "success"
    assert message.artifact["offers"][0]["total_price"] == 1360.0


def test_calendar_create_owns_idempotency_key() -> None:
    tool = create_calendar_create_tool(recording_adapter)
    assert "idempotency_key" not in tool.tool_call_schema.model_fields
    invoke_with_tool_node(tool, valid_calendar_args, tool_call_id="calendar-call")
    assert recording_adapter.last_request.idempotency_key == "native:thread-sentinel:calendar-call"
```

Also assert `limit` is absent from calendar search, contacts and lodging schemas.

- [ ] **Step 2: 运行 RED**

Run both TDD directories; expected failure is missing factory exports.

- [ ] **Step 3: 实现四个官方工厂**

Use explicit model-visible parameters and construct full domain requests. The lodging body must construct:

```python
request = LodgingSearchRequest(
    destination=destination,
    check_in=check_in,
    check_out=check_out,
    adults=adults,
    rooms=rooms,
    currency=currency,
    keywords=keywords,
    nearby_poi=nearby_poi,
    hotel_types=hotel_types,
    star_ratings=star_ratings,
    bed_types=bed_types,
    max_nightly_price=max_nightly_price,
    sort=sort,
)
```

Do not expose `limit`. The official inferred schema must remain `type=string, format=date` and the execution path must receive parsed `date` values.

- [ ] **Step 4: 更新 Plugin/导出并运行 GREEN**

Run:

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools/test_personal_travel_tools.py \
  tests/tdd/lodging-search-date-compatibility
```

Expected: PASS.

- [ ] **Step 5: 创建任务 checkpoint**

Ruff the modified modules and commit in an isolated worktree:

```bash
git commit -m "refactor: migrate personal and travel tools"
```

---

### Task 4: 迁移网站引导、购物与视觉搜图 Tool

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/website_guidance/tools.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/website_guidance/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/website_guidance/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/visual_image_search/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/visual_image_search/plugin.py`
- Create: `tests/tdd/native-builtin-tools/test_guidance_search_tools.py`

**Interfaces:**
- Produces: `create_web_page_inspect_tool(backend)`, `create_web_page_explore_tool(backend)` sharing one backend instance.
- Produces: `create_shopping_search_tool(*, search_adapter, compare_adapter)`; hide `top_k_per_need`.
- Produces: `create_visual_image_search_tool(adapter=None)`; hide `limit`.
- Metadata effects remain `read`, `dangerous`, `read`, `read` respectively.

- [ ] **Step 1: 写 schema、effect 与共享 backend RED 测试**

Create the tools from recording adapters, assert exact names/effects and hidden fields, then invoke inspect/explore and assert both calls reached the same backend object.

- [ ] **Step 2: 运行 RED**

Expected: missing `create_*` factories.

- [ ] **Step 3: 替换类并保留领域输出投影**

Move each `_execute` body to a private function and use `invoke_native_tool`. For website factories, `_tools_for(backend)` must return:

```python
return [
    create_web_page_inspect_tool(backend),
    create_web_page_explore_tool(backend),
]
```

Shopping and visual-image factories must construct their existing Request models without the hidden service limit, allowing the domain default to apply.

- [ ] **Step 4: 运行 GREEN 与 Plugin readiness tests**

Run `tests/tdd/native-builtin-tools/test_guidance_search_tools.py`; expected PASS. The current repository has no separate website/shopping TDD directory, so no additional feature suite is required in this task.

- [ ] **Step 5: 创建任务 checkpoint**

Ruff and commit in an isolated worktree:

```bash
git commit -m "refactor: migrate guidance and search tools"
```

---

### Task 5: 迁移写入、生成与持久任务 Tool

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/python_execution/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/python_execution/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/watch_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/visual_reminder_tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Create: `tests/tdd/native-builtin-tools/test_effectful_tool_factories.py`

**Interfaces:**
- Produces: `create_python_interpreter_tool(sandbox=None, *, require_enable_env=True)`; schema exposes `code`, `input_data`, hides `purpose`, `timeout_s`.
- Produces: `create_hotel_price_watch_create_tool(service)` with closure-owned service.
- Produces: `create_image_generation_tool(adapter=None)`; schema exposes only `prompt`, runtime injects user/session/Memory.
- Produces: `create_image_to_3d_tool(adapter=None)`.
- Produces: `create_visual_reminder_manage_tool(*, coordinator_store, reminder_registry)`; runtime injects session and identity.

- [ ] **Step 1: 写安全、Runtime 注入和 effect RED 测试**

```python
def test_python_native_tool_rejects_unsafe_code_before_sandbox() -> None:
    message = invoke_with_tool_node(
        create_python_interpreter_tool(recording_sandbox, require_enable_env=False),
        {"code": "import os"},
    )
    assert message.status == "error"
    assert recording_sandbox.calls == []


def test_image_generation_injects_identity_and_frozen_memory() -> None:
    tool = create_image_generation_tool(recording_adapter)
    assert set(tool.tool_call_schema.model_fields) == {"prompt"}
    invoke_with_tool_node(
        tool,
        {"prompt": "生成河畔酒店插画"},
        memory_context=["memory-sentinel"],
    )
    request = recording_adapter.last_request
    assert request.user_id == "user-sentinel"
    assert request.session_id == "thread-sentinel"
    assert request.memory_context == ["memory-sentinel"]
```

Add hotel-watch test proving the closure service is used and a standard task artifact is returned; add metadata assertions for write/generate effects.

- [ ] **Step 2: 运行 RED**

Expected: missing factories or old class behavior.

- [ ] **Step 3: 实现 effectful 工厂**

In `python_interpreter`, call `validate_python_code_safety(code)` before `sandbox.run`; convert a returned error directly to `ToolException(f"{error.code}: {error.message}")`.

In `image_generation`, construct:

```python
state = runtime.state if isinstance(runtime.state, Mapping) else {}
request = ImageGenerationRequest(
    prompt=prompt,
    user_id=authenticated_user_identity(runtime),
    session_id=runtime.execution_info.thread_id,
    memory_context=list(state.get("memory_context", ())),
)
```

In hotel watch, call the closure-owned service directly after validating trusted user/thread/run; remove the broken `context.metadata["durable_task_service"]` identity check.

- [ ] **Step 4: 更新 Plugin 并运行 GREEN**

Run the new TDD file plus `tests/tdd/studio-generated-image`; expected PASS.

- [ ] **Step 5: 创建任务 checkpoint**

Ruff and commit in an isolated worktree:

```bash
git commit -m "refactor: migrate effectful tools to native factories"
```

---

### Task 6: 迁移 Skill Tool 并保持渐进暴露 Command

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/tool.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/__init__.py`
- Modify: `src/assistant_agent/native_agent/tool_exposure.py` only if factory metadata requires a compatibility-free lookup adjustment
- Create: `tests/tdd/native-builtin-tools/test_skill_tool_factories.py`

**Interfaces:**
- Produces: `create_load_skill_tool(*, root=None)` and `create_load_skill_reference_tool(*, root=None)`.
- `load_skill_reference` reads `skill_reference_grants(runtime.state)`; no model argument may supply grants or filesystem paths.
- Existing `ProgressiveToolExposureMiddleware` must still recognize successful `load_skill` artifact and return the same standard `Command(update=...)`.

- [ ] **Step 1: 写 factory schema、grant 与 middleware RED 测试**

Test `skill_id/reference_id` schemas, successful load artifact, unauthorized reference failure, authorized reference success, and middleware grant update from the returned `ToolMessage`.

- [ ] **Step 2: 运行 RED**

Expected: missing factories.

- [ ] **Step 3: 实现两个官方工厂**

Use closure-resolved root. `load_skill_reference` must build its `ToolContext` with the current state grants before calling the existing descriptor/reference helpers. Preserve artifact keys `skill_id`, `reference_ids`, `granted_tools`, and `unavailable_tools` because middleware consumes them.

- [ ] **Step 4: 运行 GREEN 与 context lifecycle**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools/test_skill_tool_factories.py \
  tests/core/integration/test_context_lifecycle.py
```

Expected: PASS.

- [ ] **Step 5: 创建任务 checkpoint**

Ruff and commit in an isolated worktree:

```bash
git commit -m "refactor: migrate skill tools to native factories"
```

---

### Task 7: 切换全部装配与 eval，删除旧 Tool 框架

**Files:**
- Modify: `src/assistant_agent/tools/plugins/builtin/calendar_weather_contacts/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/email_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_generation/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/image_to_3d/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/local_file_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/media_inspection/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/python_execution/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/shopping/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/visual_image_search/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/web_access/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/website_guidance/plugin.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/lodging/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/skill_loading/__init__.py`
- Modify: `src/assistant_agent/tools/plugins/builtin/website_guidance/__init__.py`
- Modify: `evals/system/tools/calendar_create.py`
- Modify: `evals/system/tools/calendar_search.py`
- Modify: `evals/system/tools/file_read.py`
- Modify: `evals/system/tools/image_generation.py`
- Modify: `evals/system/tools/image_to_3d.py`
- Modify: `evals/system/tools/load_skill.py`
- Modify: `evals/system/tools/load_skill_reference.py`
- Modify: `evals/system/tools/lodging_search.py`
- Modify: `evals/system/tools/python_interpreter.py`
- Modify: `evals/system/tools/shopping_search.py`
- Delete: `src/assistant_agent/tools/base.py`
- Delete: `src/assistant_agent/tools/input_binding.py`
- Modify: `tests/core/contract/test_extension_contract.py`
- Create: `tests/tdd/native-builtin-tools/test_inventory_migration.py`

**Interfaces:**
- All internal/eval construction uses `create_*_tool` factories.
- `create_native_tool_inventory(...) -> list[BaseTool]` remains unchanged.
- No production, tests or eval source imports `ToolBase`, `RuntimeInputBinding`, `llm_hidden_input_fields` or old `*Tool` classes.

- [ ] **Step 1: 写 inventory RED 测试**

```python
def test_builtin_inventory_contains_only_official_native_tools() -> None:
    tools = asyncio.run(create_native_tool_inventory(mock_config, resources, []))
    assert tools
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert all(tool.metadata["source"] == "builtin" for tool in tools)
    assert all(tool.metadata["effect"] in {"read", "generate", "write", "dangerous"} for tool in tools)
    assert not any(type(tool).__module__ == "assistant_agent.tools.base" for tool in tools)
```

Add `EXT-001` core assertions for legal standard metadata without importing concrete business tools.

- [ ] **Step 2: 运行 RED**

Expected: old constructors/imports or metadata assertions fail.

- [ ] **Step 3: 更新所有 Plugin、eval 与导出**

Mechanically replace each class constructor/import with its named factory. Preserve Plugin readiness and shared-resource behavior. Update eval adapters to pass the same fake/real dependencies to factories.

- [ ] **Step 4: 删除旧框架并证明无遗留引用**

Delete `tools/base.py` and `tools/input_binding.py`, then run:

```bash
rg -n "ToolBase|RuntimeInputBinding|llm_hidden_input_fields|runtime_input_bindings|_native_input_model" \
  src tests/core evals/system scripts
```

Expected: no matches.

Also search every removed class name and confirm no production/test/eval imports remain.

- [ ] **Step 5: 运行 GREEN 与 core contracts**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools \
  tests/core/contract/test_tool_contract.py \
  tests/core/contract/test_extension_contract.py
```

Expected: PASS.

- [ ] **Step 6: 创建任务 checkpoint**

Ruff all modified Python files. In an isolated worktree, commit only migration files:

```bash
git commit -m "refactor: remove legacy tool base"
```

---

### Task 8: 同步 authority 并完成离线验证

**Files:**
- Modify: `docs/tool-calling-architecture.md`
- Modify: `tests/core/INVARIANTS.md`
- Verify: `docs/authority.toml`

**Interfaces:**
- Authority must state all in-process built-ins are official `@tool` factories, runtime is a hidden `ToolRuntime` parameter, and no dynamic project schema layer exists.
- Existing provider-native、MCP、HITL、retry and progressive exposure boundaries remain unchanged.

- [ ] **Step 1: 更新 authority 文档**

Replace the paragraph describing concrete `ToolBase` subclasses with the approved factory model. Remove references to runtime-owned fields being stripped by a project-generated execution schema; state that official `ToolRuntime` injection hides them directly.

Update `TOOL-001` wording to require that production in-process built-ins are official `@tool` factories while preserving the existing runtime identity and standard `ToolMessage` clauses. Do not add a new invariant ID.

- [ ] **Step 2: 运行文档 authority 校验**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  scripts/check_documentation_authority.py --repo-root .
```

Expected: valid.

- [ ] **Step 3: 运行完整 mock/offline pytest**

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q

MULTIMODAL_AGENT_PROVIDER_MODE=mock PYTHONPATH=src \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m pytest -q \
  tests/tdd/native-builtin-tools \
  tests/tdd/lodging-search-date-compatibility \
  tests/tdd/studio-generated-image
```

Expected: all selected tests PASS.

- [ ] **Step 4: 运行静态检查**

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python -m ruff check \
  src/assistant_agent/tools src/assistant_agent/native_agent \
  tests/core evals/system/tools
git diff --check
```

Expected: no errors.

- [ ] **Step 5: 验证唯一 8089 开发服务 reload**

In the main workspace only, do not start a second server. Confirm port and health/schema:

```bash
ss -ltnp | rg ':8089\b'
curl -sS -o /tmp/assistant_agent_native_tools_openapi.json -w '%{http_code}\n' \
  http://127.0.0.1:8089/openapi.json
```

Expected: one existing service and HTTP 200. If implementation ran in an isolated worktree, perform this step only after the task branch is integrated into the main workspace.

- [ ] **Step 6: 最终审查与提交决策**

Review `git status --short`, isolate unrelated user changes, and report:

```text
Core invariant: TOOL-001 and EXT-001 implementation updated; invariant semantics unchanged.
Tests: updated existing core contract tests and added temporary tests/tdd/native-builtin-tools; user may delete the TDD directory manually.
Real Provider: not called.
```

If working in an isolated clean worktree, commit the authority update:

```bash
git commit -m "docs: document native builtin tools"
```

Do not push, merge or create a PR unless the user explicitly asks.
