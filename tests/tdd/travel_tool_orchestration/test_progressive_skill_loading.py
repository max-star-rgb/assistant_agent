from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from assistant_agent.config import ProviderConfig
from assistant_agent.context.builder import build_assistant_context_pack
from assistant_agent.context.observability import (
    build_traced_assistant_context_pack,
)
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
    procedural_guidance_for_pack,
)
from assistant_agent.runtime.action_validator import ActionValidator
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.output_models import AssistantToolCall
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.session_store import InMemorySessionStore
from assistant_agent.runtime.state import AgentState
from assistant_agent.runtime.tool_executor import ToolExecutor
from assistant_agent.observability.trace_store import InMemoryTraceStore
from assistant_agent.providers.specs import ProviderCapabilities
from assistant_agent.skills.loading import (
    load_repo_skill_descriptors,
    read_registered_skill_reference,
    render_skill_guidance,
)
from assistant_agent.tools.models import RunToolCatalog, ToolSpec
from assistant_agent.tools.base import ToolBase, ToolContext
from assistant_agent.tools.models import ToolResult
from assistant_agent.tools.observation import observation_from_tool_result
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.registry import ToolRegistry


class _EmptyInput(BaseModel):
    pass


class _LodgingProbeTool(ToolBase):
    name = "lodging_search"
    description = "lodging sentinel"
    input_schema = _EmptyInput
    output_schema = _EmptyInput
    category = "read"

    def _run(self, input: _EmptyInput, context: ToolContext) -> ToolResult:
        return ToolResult(tool_name=self.name, success=True, data={})


class _DeveloperRoleAdapter:
    provider = "test"
    model = "test"
    capabilities = ProviderCapabilities(supports_developer_role=True)

    def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(provider=self.provider, response_text="unused")


def _travel_tool() -> ToolSpec:
    return ToolSpec(
        name="lodging_search",
        description="lodging sentinel",
        category="read",
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )


def _run_governed(
    tool_name: str,
    tool_input: dict,
    *,
    registry: ToolRegistry | None = None,
    state: AgentState | None = None,
):
    registry = registry or create_default_registry()
    if state is None:
        request = UserRequest(
            user_id="skill-user",
            session_id="skill-session",
            text="执行 sentinel-84",
        )
        state = AgentState.from_request(request)
    else:
        request = state.request
    state.run_tool_catalog = RunToolCatalog(
        available_tool_names=registry.list()
    )
    decision = AssistantToolCall(
        tool_name=tool_name,
        tool_input=tool_input,
    )
    validation = ActionValidator().validate(
        decision=decision,
        registry=registry,
        request=request,
        state=state,
    )
    assert validation.accepted is True
    assert validation.validated_input is not None
    return ToolExecutor(registry=registry).run_tool(
        state,
        "skill-step",
        decision.tool_name,
        decision.tool_input,
        validated_input=validation.validated_input,
    )


def _pack_with_observation(observation: dict):
    registry = create_default_registry()
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text="执行 sentinel-86",
    )
    return build_assistant_context_pack(
        state=AgentState.from_request(request),
        tool_specs=[
            _travel_tool(),
            *[
                spec
                for spec in registry.list_specs()
                if spec.name in {"load_skill", "load_skill_reference"}
            ],
        ],
        observations=[observation],
        iteration=1,
        max_iterations=5,
    )


def _compile(pack, *, supports_developer_role: bool = False):
    return PromptCompiler().compile(
        PromptCompileRequest(
            user_id=pack.request.user_id,
            session_id=pack.request.session_id,
            mode=PromptCompileMode.NATIVE_TOOL,
            user_query_fallback="fallback",
            context_pack=pack,
            observations=tuple(pack.observations),
            native_calls=(),
            tool_call_id_prefix="call_",
            supports_developer_role=supports_developer_role,
        )
    )


def _compile_system(pack) -> str:
    return _compile(pack).chat_request.messages[0]["content"]


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    reference_path: str | None = None,
) -> Path:
    skill_dir = root / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    references = (
        f'\n[references]\nguide = "{reference_path}"\n'
        if reference_path is not None
        else ""
    )
    (skill_dir / "skill.toml").write_text(
        (
            "schema_version = 1\n"
            f'skill_id = "{skill_id}"\n'
            "version = 1\n"
            'description = "Use when running a sentinel capability."\n'
            "enabled = true\n"
            "discoverable = true\n"
            "disable_model_invocation = false\n"
            'activation = "model"\n'
            'governed_tools = ["sentinel_tool"]\n'
            f"{references}"
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        (
            "# Sentinel workflow\n\n"
            "Follow the procedural sentinel guidance.\n"
        ),
        encoding="utf-8",
    )
    return skill_dir


def test_default_registry_exposes_governed_progressive_skill_loaders() -> None:
    registry = create_default_registry()

    assert "load_skill" in registry.list()
    assert "load_skill_reference" in registry.list()
    assert registry.get_spec("load_skill").category == "read"
    assert registry.get_spec("load_skill_reference").category == "read"


def test_automatic_context_contains_only_short_skill_activation_summary() -> None:
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text="执行 sentinel-85",
    )
    pack = build_assistant_context_pack(
        state=AgentState.from_request(request),
        tool_specs=[
            _travel_tool(),
            *[
                spec
                for spec in create_default_registry().list_specs()
                if spec.name in {"load_skill", "load_skill_reference"}
            ],
        ],
        iteration=0,
        max_iterations=5,
    )

    summary = next(
        section
        for section in pack.context_sections
        if section.kind == "skill_summary"
    )
    assert pack.active_skill_ids == []
    assert pack.discoverable_skill_ids == ["travel-tool-orchestration"]
    assert len(summary.content) < 300
    assert "load_skill" not in summary.content
    assert "酒店比较" in summary.content
    assert "按天行程决策" in summary.content
    assert "# 可用 Skill" not in summary.content


def test_load_skill_returns_complete_travel_planning_workflow() -> None:
    result = _run_governed(
        "load_skill",
        {"skill_id": "travel-tool-orchestration"},
    )

    assert result.success is True
    assert result.data is not None
    assert result.model_observation is not None
    assert result.model_observation["skill_id"] == "travel-tool-orchestration"
    assert result.model_observation["reference_ids"] == []
    assert "content" not in result.model_observation
    assert result.data["content"].startswith("# 旅行决策与行程编排")
    assert "Provider-native 联网搜索" in result.data["content"]
    assert "按天、按时间段" in result.data["content"]
    assert "已核验信息" in result.data["content"]
    assert "攻略性建议" in result.data["content"]
    assert "待确认事项" in result.data["content"]
    assert "## 决策规则" in result.data["content"]
    assert "## 执行流程" in result.data["content"]
    assert "## 常见错误" in result.data["content"]
    assert "## 完成前检查" in result.data["content"]
    assert [
        line
        for line in result.data["content"].splitlines()
        if line.startswith("## ")
    ] == [
        "## 适用场景",
        "## 不适用场景",
        "## 决策规则",
        "## 信息来源速查",
        "## 执行流程",
        "## 输出契约",
        "## 常见错误",
        "## 完成前检查",
    ]


def test_load_skill_reference_requires_successful_load_in_same_run() -> None:
    result = _run_governed(
        "load_skill_reference",
        {
            "skill_id": "travel-tool-orchestration",
            "reference_id": "missing-reference",
        },
    )

    assert result.success is False
    assert result.error == "skill_reference_not_loaded"


def test_load_skill_reference_rejects_id_not_returned_by_loaded_skill() -> None:
    registry = create_default_registry()
    state = AgentState.from_request(
        UserRequest(
            user_id="skill-user",
            session_id="skill-session",
            text="执行 sentinel-84-unregistered",
        )
    )
    loaded = _run_governed(
        "load_skill",
        {"skill_id": "travel-tool-orchestration"},
        registry=registry,
        state=state,
    )
    assert loaded.success is True

    result = _run_governed(
        "load_skill_reference",
        {
            "skill_id": "travel-tool-orchestration",
            "reference_id": "private-file",
        },
        registry=registry,
        state=state,
    )

    assert result.success is False
    assert result.error == "skill_reference_not_loaded"
    assert result.model_observation == {
        "status": "failed",
        "summary": "该 reference 未由本次运行中成功的 load_skill 返回。",
        "errors": [
            {
                "code": "skill_reference_not_loaded",
                "message": "该 reference 未由本次运行中成功的 load_skill 返回。",
                "recoverable": False,
            }
        ],
    }


def test_successful_load_skill_is_promoted_from_registered_source() -> None:
    result = _run_governed(
        "load_skill",
        {"skill_id": "travel-tool-orchestration"},
    )
    assert result.model_observation is not None
    result.model_observation["content"] = "observation-injection-sentinel"
    observation = observation_from_tool_result(result).model_dump(mode="json")

    pack = _pack_with_observation(observation)

    body = next(
        section
        for section in pack.context_sections
        if section.kind == "skill_body"
    )
    assert all(
        section.kind != "skill_summary"
        or section.title != "travel-tool-orchestration"
        for section in pack.context_sections
    )
    assert body.authority == "procedural_guidance"
    assert body.content.startswith("# 旅行决策与行程编排")
    assert "## 决策规则" in body.content
    assert "## 执行流程" in body.content
    assert "observation-injection-sentinel" not in body.content
    assert body.content in _compile_system(pack)
    system_prompt = _compile_system(pack)
    assert "<loaded_skills>" not in system_prompt
    assert (
        '<loaded_skill id="travel-tool-orchestration" version="3">'
        in system_prompt
    )
    assert system_prompt.index("<loaded_skill ") < system_prompt.index(
        body.content
    )
    assert system_prompt.index(body.content) < system_prompt.index(
        "</loaded_skill>"
    )


def test_supported_provider_compiles_loaded_skill_as_developer_message() -> None:
    result = _run_governed(
        "load_skill",
        {"skill_id": "travel-tool-orchestration"},
    )
    observation = observation_from_tool_result(result).model_dump(mode="json")
    pack = _pack_with_observation(observation)
    body = next(
        section
        for section in pack.context_sections
        if section.kind == "skill_body"
    )

    compiled = _compile(pack, supports_developer_role=True)

    assert body.content not in compiled.chat_request.messages[0]["content"]
    assert "## 技能生命周期" not in compiled.chat_request.messages[0]["content"]
    assert compiled.chat_request.messages[1] == {
        "role": "developer",
        "content": procedural_guidance_for_pack(pack),
    }


def test_skill_loader_rejects_symlinked_skill_directory(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    outside_root = tmp_path / "outside"
    _write_skill(outside_root, "linked-skill")
    (repo_root / "skills").mkdir(parents=True)
    (repo_root / "skills" / "linked-skill").symlink_to(
        outside_root / "skills" / "linked-skill",
        target_is_directory=True,
    )

    catalog = load_repo_skill_descriptors(repo_root)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == [
        "skill_symlink_not_allowed"
    ]


def test_manifest_skill_full_body_remains_available_after_loading(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    skill_dir = _write_skill(repo_root, "legacy-skill")
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8")
        + "\n## 激活后流程\n\nlegacy-activation-sentinel\n"
        + "\n## 输入准备\n\n- sentinel_tool: query\n"
        + "\n## 安全示例\n\n- legacy-safe-example-sentinel\n"
        + "\n## 运行约束\n\n- legacy-runtime-sentinel\n",
        encoding="utf-8",
    )

    catalog = load_repo_skill_descriptors(repo_root)

    assert catalog.issues == []
    assert len(catalog.descriptors) == 1
    content = render_skill_guidance(catalog.descriptors[0])
    assert "legacy-activation-sentinel" in content
    assert "legacy-safe-example-sentinel" in content
    assert "legacy-runtime-sentinel" in content


def test_skill_loader_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    skill_dir = _write_skill(repo_root, "future-schema-skill")
    manifest_file = skill_dir / "skill.toml"
    content = manifest_file.read_text(encoding="utf-8").replace(
        "schema_version = 1",
        "schema_version = 2",
    )
    manifest_file.write_text(content, encoding="utf-8")

    catalog = load_repo_skill_descriptors(repo_root)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == [
        "invalid_skill_manifest"
    ]


def test_skill_loader_rejects_malformed_manifest_version(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    skill_dir = _write_skill(repo_root, "malformed-version-skill")
    manifest_file = skill_dir / "skill.toml"
    content = manifest_file.read_text(encoding="utf-8").replace(
        "version = 1",
        'version = "two"',
    )
    manifest_file.write_text(content, encoding="utf-8")

    catalog = load_repo_skill_descriptors(repo_root)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == [
        "invalid_skill_manifest"
    ]


def test_skill_loader_accepts_new_procedure_version(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    skill_dir = _write_skill(repo_root, "future-version-skill")
    manifest_file = skill_dir / "skill.toml"
    content = manifest_file.read_text(encoding="utf-8").replace(
        "\nversion = 1\n",
        "\nversion = 3\n",
    )
    manifest_file.write_text(content, encoding="utf-8")

    catalog = load_repo_skill_descriptors(repo_root)

    assert catalog.issues == []
    assert [descriptor.manifest_version for descriptor in catalog.descriptors] == [3]


def test_skill_loader_rejects_symlinked_reference(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    skill_dir = _write_skill(
        repo_root,
        "safe-skill",
        reference_path="references/guide.md",
    )
    (skill_dir / "references").mkdir()
    outside_reference = tmp_path / "outside-guide.md"
    outside_reference.write_text("linked-reference-sentinel", encoding="utf-8")
    (skill_dir / "references" / "guide.md").symlink_to(outside_reference)

    catalog = load_repo_skill_descriptors(repo_root)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == [
        "reference_symlink_not_allowed"
    ]


def test_reference_read_rejects_skills_ancestor_swapped_to_symlink(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    skill_dir = _write_skill(
        repo_root,
        "safe-skill",
        reference_path="references/guide.md",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text(
        "original-reference-sentinel",
        encoding="utf-8",
    )
    descriptor = load_repo_skill_descriptors(repo_root).descriptors[0]

    external_root = tmp_path / "external"
    external_skill_dir = _write_skill(
        external_root,
        "safe-skill",
        reference_path="references/guide.md",
    )
    (external_skill_dir / "references").mkdir()
    (external_skill_dir / "references" / "guide.md").write_text(
        "ancestor-symlink-content",
        encoding="utf-8",
    )
    (repo_root / "skills").rename(repo_root / "original-skills")
    (repo_root / "skills").symlink_to(
        external_root / "skills",
        target_is_directory=True,
    )

    content = read_registered_skill_reference(
        repo_root,
        descriptor,
        "guide",
    )

    assert content is None


def test_successful_load_promotes_registered_but_inactive_skill() -> None:
    result = _run_governed(
        "load_skill",
        {"skill_id": "travel-tool-orchestration"},
    )
    observation = observation_from_tool_result(result).model_dump(mode="json")
    registry = create_default_registry()
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text="执行 sentinel-87",
    )

    pack = build_assistant_context_pack(
        state=AgentState.from_request(request),
        tool_specs=[
            spec
            for spec in registry.list_specs()
            if spec.name in {"load_skill", "load_skill_reference"}
        ],
        observations=[observation],
        iteration=1,
        max_iterations=5,
    )

    assert pack.active_skill_ids == []
    assert any(
        section.kind == "skill_body"
        and section.title == "travel-tool-orchestration"
        for section in pack.context_sections
    )


def test_finalize_context_report_uses_actual_empty_tool_request() -> None:
    trace_store = InMemoryTraceStore()
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text="执行 sentinel-88",
    )
    state = AgentState.from_request(request)

    build_traced_assistant_context_pack(
        trace_store=trace_store,
        trace_id=state.trace_id,
        node_name="assistant",
        state=state,
        tool_specs=[
            _travel_tool(),
            *[
                spec
                for spec in create_default_registry().list_specs()
                if spec.name in {"load_skill", "load_skill_reference"}
            ],
        ],
        iteration=0,
        max_iterations=5,
        answer_only=True,
    )

    event = next(
        item
        for item in trace_store.list_by_run(state.run_id)
        if item.canonical_event == "context.build.finished"
    )
    report = event.output_summary["context_report_v2"]
    assert report.get("selected_tool_names", []) == []
    assert "tool_schema" not in report["sections"]


def test_traced_context_build_reports_developer_skill_guidance() -> None:
    trace_store = InMemoryTraceStore()
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text="执行 sentinel-developer-trace",
    )
    state = AgentState.from_request(request)

    build_traced_assistant_context_pack(
        trace_store=trace_store,
        trace_id=state.trace_id,
        node_name="assistant",
        state=state,
        tool_specs=[
            _travel_tool(),
            *[
                spec
                for spec in create_default_registry().list_specs()
                if spec.name in {"load_skill", "load_skill_reference"}
            ],
        ],
        iteration=0,
        max_iterations=5,
        supports_developer_role=True,
    )

    event = next(
        item
        for item in trace_store.list_by_run(state.run_id)
        if item.canonical_event == "context.build.finished"
    )
    report = event.output_summary["context_report_v2"]
    assert report["sections"]["developer_prompt"]["chars"] > 0


def test_durable_quantum_preserves_developer_role_capability() -> None:
    registry = ToolRegistry()
    registry.register(_LodgingProbeTool())
    default_registry = create_default_registry()
    registry.register(default_registry.get("load_skill"))
    registry.register(default_registry.get("load_skill_reference"))
    registry.seal()
    runtime = AgentGraphRuntime(
        registry=registry,
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=_DeveloperRoleAdapter(),
        session_store=InMemorySessionStore(),
    )
    request = UserRequest(
        user_id="skill-user",
        session_id="skill-session",
        text="执行 sentinel-durable-developer",
        task_execution_mode="durable",
    )
    state = AgentState.from_request(request)

    try:
        compiled = runtime._durable_quantum_chat_request(request, state=state)
    finally:
        runtime.close()

    assert compiled is not None
    assert any(
        message["role"] == "developer"
        and "travel-tool-orchestration" in message["content"]
        for message in compiled.messages
    )
