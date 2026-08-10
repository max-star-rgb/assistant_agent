from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event, Lock
from typing import Literal

import pytest
from pydantic import ValidationError

from assistant_agent.skills.loading import (
    default_repo_root,
    load_repo_skill_descriptors,
)
from assistant_agent.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.context.builder import build_assistant_context_pack
from assistant_agent.context.prompt_compiler import (
    PromptCompileMode,
    PromptCompileRequest,
    PromptCompiler,
)
from assistant_agent.runtime.capability_grants import (
    ContextToolsetGrant,
    DeferredToolsetGrant,
    SkillGrant,
)
from assistant_agent.runtime.session_store import (
    InMemorySessionStore,
    JsonlSessionStore,
)
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.chat_adapter import ChatRequest, ChatResult
from assistant_agent.runtime.output_models import NativeToolCall
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.runtime.state import AgentState
from assistant_agent.config import ProviderConfig
from assistant_agent.tools.models import ToolResult, ToolSpec
from assistant_agent.tools.plugins.registry_factory import create_default_registry
from assistant_agent.tools.plugins.builtin.skill_loading.tool import LoadSkillTool


class _ScriptedChatAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, results: list[ChatResult]) -> None:
        self._results = iter(results)
        self.requests: list[ChatRequest] = []

    def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        return next(self._results)


def _request_tool_names(request: ChatRequest) -> list[str]:
    names: list[str] = []
    for payload in request.tools:
        function = payload.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(function["name"])
    return names


def _write_manifest_skill(
    root: Path,
    skill_id: str,
    *,
    activation: str = "model",
    body: str = "# Sentinel workflow\n\nFollow the governed procedure.",
    governed_tools: tuple[str, ...] = ("sentinel_search",),
    manifest_skill_id: str | None = None,
    references: dict[str, str] | None = None,
) -> Path:
    skill_dir = root / "skills" / skill_id
    skill_dir.mkdir(parents=True)
    tool_items = ", ".join(f'"{name}"' for name in governed_tools)
    reference_lines = "\n".join(
        f'{reference_id} = "{path}"'
        for reference_id, path in (references or {}).items()
    )
    manifest = (
        "schema_version = 1\n"
        f'skill_id = "{manifest_skill_id or skill_id}"\n'
        "version = 3\n"
        f'description = "Use for {skill_id} sentinel tasks."\n'
        "enabled = true\n"
        f"discoverable = {'false' if activation == 'context' else 'true'}\n"
        f"disable_model_invocation = {'true' if activation == 'context' else 'false'}\n"
        f'activation = "{activation}"\n'
        f"governed_tools = [{tool_items}]\n"
    )
    if reference_lines:
        manifest += f"\n[references]\n{reference_lines}\n"
    (skill_dir / "skill.toml").write_text(manifest, encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def test_manifest_loader_keeps_machine_contract_out_of_skill_markdown(
    tmp_path: Path,
) -> None:
    _write_manifest_skill(
        tmp_path,
        "sentinel-skill",
        activation="context",
        governed_tools=("sentinel_search", "sentinel_read"),
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.issues == []
    assert len(catalog.descriptors) == 1
    descriptor = catalog.descriptors[0]
    assert descriptor.name == "sentinel-skill"
    assert descriptor.manifest_version == 3
    assert descriptor.activation == "context"
    assert descriptor.discoverable is False
    assert descriptor.disable_model_invocation is True
    assert descriptor.governed_tools == ["sentinel_search", "sentinel_read"]
    assert descriptor.body == "# Sentinel workflow\n\nFollow the governed procedure."


def test_manifest_loader_rejects_directory_id_mismatch(tmp_path: Path) -> None:
    _write_manifest_skill(
        tmp_path,
        "sentinel-skill",
        manifest_skill_id="different-skill",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["skill_id_mismatch"]


def test_manifest_loader_rejects_machine_sections_in_skill_markdown(
    tmp_path: Path,
) -> None:
    _write_manifest_skill(
        tmp_path,
        "sentinel-skill",
        body=(
            "# Sentinel workflow\n\n"
            "Follow the governed procedure.\n\n"
            "## 受治理工具\n\n- sentinel_search\n"
        ),
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == [
        "skill_markdown_contains_machine_contract"
    ]


def test_manifest_loader_requires_skill_toml(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "sentinel-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Sentinel workflow\n\nFollow the governed procedure.",
        encoding="utf-8",
    )

    catalog = load_repo_skill_descriptors(tmp_path)

    assert catalog.descriptors == []
    assert [issue.code for issue in catalog.issues] == ["missing_skill_manifest"]


def test_concrete_capability_grants_preserve_typed_subjects() -> None:
    from assistant_agent.runtime.capability_grants import (
        ContextToolsetGrant,
        DeferredToolsetGrant,
        SkillGrant,
        validate_capability_grant,
    )

    skill = validate_capability_grant(
        {
            "source": "skill",
            "grant_id": "skill:sentinel-skill",
            "skill_id": "sentinel-skill",
            "tool_names": ["sentinel_search"],
        }
    )
    context_toolset = validate_capability_grant(
        {
            "source": "context",
            "grant_id": "context:visual-context",
            "toolset_id": "visual-context",
            "tool_names": ["media_inspect"],
        }
    )
    deferred_toolset = validate_capability_grant(
        {
            "source": "tool_search",
            "grant_id": "tool-search:workspace",
            "toolset_id": "workspace",
            "tool_names": ["email_search"],
        }
    )
    legacy_context_toolset = validate_capability_grant(
        {
            "source": "context",
            "grant_id": "context:legacy-visual-context",
            "skill_id": "legacy-visual-context",
            "tool_names": ["media_inspect"],
        }
    )

    assert isinstance(skill, SkillGrant)
    assert skill.capability_id == "sentinel-skill"
    with pytest.raises(ValidationError, match="frozen"):
        skill.source = "context"  # type: ignore[assignment]
    assert isinstance(context_toolset, ContextToolsetGrant)
    assert context_toolset.capability_id == "visual-context"
    assert isinstance(deferred_toolset, DeferredToolsetGrant)
    assert deferred_toolset.capability_id == "workspace"
    assert isinstance(legacy_context_toolset, ContextToolsetGrant)
    assert legacy_context_toolset.model_dump(mode="json") == {
        "source": "context",
        "grant_id": "context:legacy-visual-context",
        "agent_id": "agent.default",
        "tool_names": ["media_inspect"],
        "toolset_id": "legacy-visual-context",
    }


def test_context_toolset_grant_rejects_conflicting_legacy_subjects() -> None:
    from assistant_agent.runtime.capability_grants import validate_capability_grant

    with pytest.raises(ValueError, match="conflicting context Toolset subjects"):
        validate_capability_grant(
            {
                "source": "context",
                "grant_id": "context:visual-context",
                "toolset_id": "visual-context",
                "skill_id": "different-context",
                "tool_names": ["media_inspect"],
            }
        )


def test_capability_grant_boundaries_reject_unregistered_runtime_types(
    tmp_path: Path,
) -> None:
    from assistant_agent.runtime.capability_grants import (
        CapabilityGrant,
        validate_capability_grant,
    )

    class UnregisteredSkillGrant(CapabilityGrant):
        source: Literal["skill"] = "skill"
        skill_id: str

        @property
        def capability_id(self) -> str:
            return self.skill_id

    unregistered = UnregisteredSkillGrant(
        grant_id="skill:sentinel-skill",
        skill_id="sentinel-skill",
        tool_names=["sentinel_search"],
    )
    parsed = validate_capability_grant(unregistered)

    assert type(parsed) is SkillGrant

    state = AgentState.from_request(
        UserRequest(user_id="owner-a", session_id="session-a", text="hello")
    )
    state.upsert_capability_grant(unregistered)  # type: ignore[arg-type]
    assert type(state.capability_grants[0]) is SkillGrant

    _write_manifest_skill(tmp_path, "sentinel-skill")
    selection = select_prompt_tool_specs(
        UserRequest(user_id="owner-a", session_id="session-a", text="hello"),
        [ToolSpec(name="sentinel_search"), ToolSpec(name="load_skill")],
        skill_catalog=load_repo_skill_descriptors(tmp_path),
        capability_grants=[unregistered],  # type: ignore[list-item]
    )
    assert "sentinel_search" not in selection.run_tool_catalog.available_tool_names

    _write_manifest_skill(
        tmp_path,
        "context-toolset",
        activation="context",
        governed_tools=("context_search",),
    )
    corrupted_skill = SkillGrant(
        grant_id="skill:context-toolset",
        skill_id="context-toolset",
        tool_names=["context_search"],
    ).model_copy(update={"source": "context"})
    corrupted_selection = select_prompt_tool_specs(
        UserRequest(user_id="owner-a", session_id="session-a", text="hello"),
        [ToolSpec(name="context_search"), ToolSpec(name="load_skill")],
        skill_catalog=load_repo_skill_descriptors(tmp_path),
        capability_grants=[corrupted_skill],
    )
    assert (
        "context_search"
        not in corrupted_selection.run_tool_catalog.available_tool_names
    )


def test_session_store_upserts_capability_grants_with_owner_isolation() -> None:
    store = InMemorySessionStore()
    first_grant = {
        "source": "skill",
        "grant_id": "skill:sentinel-skill",
        "agent_id": "agent.default",
        "skill_id": "sentinel-skill",
        "tool_names": ["sentinel_search"],
    }
    replacement_grant = {
        **first_grant,
        "tool_names": ["sentinel_search", "sentinel_read"],
    }

    store.grant_capability(
        user_id="owner-a",
        session_id="shared-session",
        grant=first_grant,
    )
    record = store.grant_capability(
        user_id="owner-a",
        session_id="shared-session",
        grant=replacement_grant,
    )

    assert [grant.model_dump(mode="json") for grant in record.capability_grants] == [
        replacement_grant
    ]
    assert "capability_grants" not in record.model_dump(mode="json")
    assert store.get("owner-b", "shared-session") is None


def test_jsonl_session_store_restores_capability_grants(tmp_path: Path) -> None:
    path = tmp_path / "sessions.jsonl"
    JsonlSessionStore(path).grant_capability(
        user_id="owner-a",
        session_id="session-a",
        grant={
            "source": "skill",
            "grant_id": "skill:sentinel-skill",
            "agent_id": "agent.default",
            "skill_id": "sentinel-skill",
            "tool_names": ["sentinel_search"],
        },
    )

    restored = JsonlSessionStore(path).get("owner-a", "session-a")

    assert restored is not None
    assert [grant.grant_id for grant in restored.capability_grants] == [
        "skill:sentinel-skill"
    ]
    assert isinstance(restored.capability_grants[0], SkillGrant)


def test_jsonl_session_store_migrates_legacy_context_grant_subject(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.jsonl"
    path.write_text(
        json.dumps(
            {
                "user_id": "owner-a",
                "session_id": "session-a",
                "capability_grants": [
                    {
                        "source": "context",
                        "grant_id": "context:visual-context",
                        "agent_id": "agent.default",
                        "skill_id": "visual-context",
                        "tool_names": ["media_inspect"],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    restored = JsonlSessionStore(path).get("owner-a", "session-a")

    assert restored is not None
    assert isinstance(restored.capability_grants[0], ContextToolsetGrant)
    assert restored.capability_grants[0].toolset_id == "visual-context"


def test_session_grants_are_atomic_across_parallel_runs(monkeypatch) -> None:
    _install_parallel_grant_probe(monkeypatch)
    store = InMemorySessionStore()

    def grant(skill_id: str) -> None:
        store.grant_capability(
            user_id="owner-a",
            session_id="session-a",
            grant={
                "source": "skill",
                "grant_id": f"skill:{skill_id}",
                "skill_id": skill_id,
                "tool_names": [f"{skill_id}_tool"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(grant, ["alpha-skill", "beta-skill"]))

    record = store.get("owner-a", "session-a")
    assert record is not None
    assert {item.grant_id for item in record.capability_grants} == {
        "skill:alpha-skill",
        "skill:beta-skill",
    }


def test_jsonl_grants_are_atomic_across_store_instances(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_parallel_grant_probe(monkeypatch)
    path = tmp_path / "parallel-sessions.jsonl"
    stores = [JsonlSessionStore(path), JsonlSessionStore(path)]

    def grant(index: int) -> None:
        skill_id = f"skill-{index}"
        stores[index].grant_capability(
            user_id="owner-a",
            session_id="session-a",
            grant={
                "source": "skill",
                "grant_id": f"skill:{skill_id}",
                "skill_id": skill_id,
                "tool_names": [f"{skill_id}_tool"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(grant, [0, 1]))

    record = JsonlSessionStore(path).get("owner-a", "session-a")
    assert record is not None
    assert {item.grant_id for item in record.capability_grants} == {
        "skill:skill-0",
        "skill:skill-1",
    }


def _install_parallel_grant_probe(monkeypatch) -> None:
    from assistant_agent.runtime import session_store as session_store_module

    original_grant_record = session_store_module._grant_record
    entered_count = 0
    entered_lock = Lock()
    both_entered = Event()

    def synchronized_grant_record(record, grant):
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
        both_entered.wait(timeout=0.1)
        return original_grant_record(record, grant)

    monkeypatch.setattr(
        session_store_module,
        "_grant_record",
        synchronized_grant_record,
    )


def test_capability_grants_do_not_cross_agent_identity(tmp_path: Path) -> None:
    from assistant_agent.runtime.capability_grants import CapabilityGrantController

    _write_manifest_skill(tmp_path, "sentinel-skill")
    store = InMemorySessionStore()
    store.grant_capability(
        user_id="owner-a",
        session_id="session-a",
        grant={
            "source": "skill",
            "grant_id": "skill:sentinel-skill",
            "agent_id": "agent-a",
            "skill_id": "sentinel-skill",
            "tool_names": ["sentinel_search"],
        },
    )
    state = AgentState.from_request(
        UserRequest(
            user_id="owner-a",
            session_id="session-a",
            text="sentinel request",
        ),
        agent_id="agent-b",
    )

    CapabilityGrantController(
        session_store=store,
        skill_root=tmp_path,
    ).prepare_run(state, [ToolSpec(name="sentinel_search")])

    assert state.capability_grants == []
    assert state.session_restored_grant_ids == []


def test_grant_controller_rebuilds_restored_skill_from_current_manifest(
    tmp_path: Path,
) -> None:
    from assistant_agent.runtime.capability_grants import CapabilityGrantController

    _write_manifest_skill(
        tmp_path,
        "sentinel-skill",
        governed_tools=("sentinel_search", "sentinel_read"),
    )
    store = InMemorySessionStore()
    store.grant_capability(
        user_id="owner-a",
        session_id="session-a",
        grant={
            "source": "skill",
            "grant_id": "skill:sentinel-skill",
            "skill_id": "sentinel-skill",
            "tool_names": ["stale_tool_name"],
        },
    )
    state = AgentState.from_request(
        UserRequest(
            user_id="owner-a",
            session_id="session-a",
            text="sentinel request",
        )
    )
    controller = CapabilityGrantController(
        session_store=store,
        skill_root=tmp_path,
    )

    controller.prepare_run(
        state,
        [
            ToolSpec(name="sentinel_search"),
            ToolSpec(name="sentinel_read"),
        ],
    )

    assert state.session_restored_grant_ids == ["skill:sentinel-skill"]
    assert [grant.model_dump(mode="json") for grant in state.capability_grants] == [
        {
            "source": "skill",
            "grant_id": "skill:sentinel-skill",
            "agent_id": "agent.default",
            "skill_id": "sentinel-skill",
            "tool_names": ["sentinel_search", "sentinel_read"],
        }
    ]


def test_grant_controller_promotes_successful_load_skill_result(
    tmp_path: Path,
) -> None:
    from assistant_agent.runtime.capability_grants import CapabilityGrantController

    _write_manifest_skill(tmp_path, "sentinel-skill")
    store = InMemorySessionStore()
    state = AgentState.from_request(
        UserRequest(
            user_id="owner-a",
            session_id="session-a",
            text="sentinel request",
        )
    )
    controller = CapabilityGrantController(
        session_store=store,
        skill_root=tmp_path,
    )

    controller.handle_tool_result(
        state,
        ToolResult(
            tool_name="load_skill",
            success=True,
            data={"skill_id": "sentinel-skill"},
        ),
    )

    assert [grant.grant_id for grant in state.capability_grants] == [
        "skill:sentinel-skill"
    ]
    assert isinstance(state.capability_grants[0], SkillGrant)
    stored = store.get("owner-a", "session-a")
    assert stored is not None
    assert [grant.grant_id for grant in stored.capability_grants] == [
        "skill:sentinel-skill"
    ]
    assert isinstance(stored.capability_grants[0], SkillGrant)


def test_catalog_hides_claimed_tools_until_capability_is_granted(
    tmp_path: Path,
) -> None:
    _write_manifest_skill(tmp_path, "sentinel-skill")
    catalog = load_repo_skill_descriptors(tmp_path)
    request = UserRequest(
        user_id="owner-a",
        session_id="session-a",
        text="sentinel request",
        metadata={
            "tool_visibility": {
                "enabled_skills": ["sentinel-skill"],
            }
        },
    )
    tool_specs = [
        ToolSpec(name="sentinel_search"),
        ToolSpec(name="unclaimed_tool"),
        ToolSpec(name="load_skill"),
    ]

    initial = select_prompt_tool_specs(
        request,
        tool_specs,
        skill_catalog=catalog,
        capability_grants=[],
    )
    activated = select_prompt_tool_specs(
        request,
        tool_specs,
        skill_catalog=catalog,
        capability_grants=[
            SkillGrant(
                grant_id="skill:sentinel-skill",
                skill_id="sentinel-skill",
                tool_names=["sentinel_search"],
            )
        ],
    )

    assert initial.run_tool_catalog.available_tool_names == [
        "unclaimed_tool",
        "load_skill",
    ]
    assert initial.discoverable_skill_ids == ["sentinel-skill"]
    assert initial.active_skill_ids == []
    assert activated.run_tool_catalog.available_tool_names == [
        "sentinel_search",
        "unclaimed_tool",
        "load_skill",
    ]
    assert activated.discoverable_skill_ids == []
    assert activated.active_skill_ids == ["sentinel-skill"]
    assert activated.skill_granted_tool_names == ["sentinel_search"]


def test_tool_search_source_is_reserved_but_cannot_grant_tools(
    tmp_path: Path,
) -> None:
    _write_manifest_skill(tmp_path, "sentinel-skill")
    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="owner-a",
            session_id="session-a",
            text="sentinel request",
        ),
        [ToolSpec(name="sentinel_search"), ToolSpec(name="load_skill")],
        skill_catalog=load_repo_skill_descriptors(tmp_path),
        capability_grants=[
            DeferredToolsetGrant(
                grant_id="tool-search:sentinel",
                toolset_id="sentinel",
                tool_names=["sentinel_search"],
            )
        ],
    )

    assert selection.run_tool_catalog.available_tool_names == ["load_skill"]
    assert selection.capability_grant_ids == []


def test_trusted_worker_catalogs_do_not_project_session_skills(
    tmp_path: Path,
) -> None:
    _write_manifest_skill(tmp_path, "sentinel-skill")
    catalog = load_repo_skill_descriptors(tmp_path)
    grant = SkillGrant(
        grant_id="skill:sentinel-skill",
        skill_id="sentinel-skill",
        tool_names=["sentinel_search"],
    )
    worker_metadata = [
        {
            "_trusted_workflow_assignment": {"work_item_id": "sentinel"},
            "_trusted_workflow_allowed_tools": ["sentinel_search"],
        },
        {
            "_trusted_durable_execution": True,
            "ready_tool_names": ["sentinel_search"],
        },
    ]

    for metadata in worker_metadata:
        selection = select_prompt_tool_specs(
            UserRequest(
                user_id="owner-a",
                session_id="session-a",
                text="sentinel worker request",
                metadata=metadata,
            ),
            [ToolSpec(name="sentinel_search"), ToolSpec(name="load_skill")],
            skill_catalog=catalog,
            capability_grants=[grant],
        )

        assert selection.run_tool_catalog.available_tool_names == [
            "sentinel_search"
        ]
        assert selection.active_skill_ids == []
        assert selection.discoverable_skill_ids == []
        assert selection.capability_grant_ids == []


def test_repo_skills_use_model_or_structured_context_activation() -> None:
    catalog = load_repo_skill_descriptors(default_repo_root())

    assert catalog.issues == []
    assert {
        descriptor.name: descriptor.activation
        for descriptor in catalog.descriptors
    } == {
        "travel-tool-orchestration": "model",
        "visual-context": "context",
        "visual-creation": "model",
        "workspace-communications": "model",
    }
    assert all(
        "## 受治理工具" not in descriptor.body
        and not descriptor.body.startswith("---")
        for descriptor in catalog.descriptors
    )


def test_improvement_replacement_reuses_current_machine_manifest() -> None:
    from assistant_agent.improvement.evaluator import (
        _load_replacement_descriptor,
    )

    descriptor = _load_replacement_descriptor(
        default_repo_root(),
        "travel-tool-orchestration",
        "# 更新后的旅行流程\n\n只替换程序性指导。",
    )

    assert descriptor is not None
    assert descriptor.manifest_version == 3
    assert "lodging_search" in descriptor.governed_tools
    assert descriptor.body == "# 更新后的旅行流程\n\n只替换程序性指导。"


def test_context_builder_renders_cards_before_grant_and_body_after_grant() -> None:
    registry = create_default_registry()
    request = UserRequest(
        user_id="owner-a",
        session_id="session-a",
        text="sentinel request",
    )
    initial_state = AgentState.from_request(request)

    initial = build_assistant_context_pack(
        state=initial_state,
        tool_specs=registry.list_specs(),
        iteration=0,
        max_iterations=5,
    )
    activated_state = AgentState.from_request(request)
    activated_state.upsert_capability_grant(
        SkillGrant(
            grant_id="skill:travel-tool-orchestration",
            skill_id="travel-tool-orchestration",
            tool_names=["lodging_search"],
        )
    )
    activated = build_assistant_context_pack(
        state=activated_state,
        tool_specs=registry.list_specs(),
        iteration=1,
        max_iterations=5,
    )

    assert "lodging_search" not in initial.run_tool_catalog.available_tool_names
    assert {
        section.title
        for section in initial.context_sections
        if section.kind == "skill_summary"
    } == {
        "travel-tool-orchestration",
        "visual-creation",
        "workspace-communications",
    }
    assert activated.active_skill_ids == ["travel-tool-orchestration"]
    assert "lodging_search" in activated.run_tool_catalog.available_tool_names
    travel_body = next(
        section
        for section in activated.context_sections
        if section.kind == "skill_body"
        and section.title == "travel-tool-orchestration"
    )
    assert travel_body.content.startswith("# 旅行决策与行程编排")
    assert all(
        section.title != "travel-tool-orchestration"
        for section in activated.context_sections
        if section.kind == "skill_summary"
    )


def test_structured_image_context_activates_visual_toolset_without_skill_body() -> None:
    registry = create_default_registry()
    store = InMemorySessionStore()
    request = UserRequest(
        user_id="owner-a",
        session_id="session-a",
        text="unrelated sentinel text",
        image_ids=["https://example.test/sentinel.png"],
    )
    state = AgentState.from_request(request)
    from assistant_agent.runtime.capability_grants import CapabilityGrantController

    controller = CapabilityGrantController(
        session_store=store,
        skill_root=default_repo_root(),
    )
    controller.prepare_run(state, registry.list_specs())

    pack = build_assistant_context_pack(
        state=state,
        tool_specs=registry.list_specs(),
        iteration=0,
        max_iterations=5,
    )

    assert "context:visual-context" in [
        grant.grant_id for grant in state.capability_grants
    ]
    visual_grant = next(
        grant
        for grant in state.capability_grants
        if grant.grant_id == "context:visual-context"
    )
    assert isinstance(visual_grant, ContextToolsetGrant)
    assert visual_grant.toolset_id == "visual-context"
    assert "media_inspect" in pack.run_tool_catalog.available_tool_names
    assert "visual_image_search" in pack.run_tool_catalog.available_tool_names
    assert "visual-context" not in pack.active_skill_ids
    assert all(
        section.kind != "skill_body" or section.title != "visual-context"
        for section in pack.context_sections
    )


def test_load_skill_rejects_context_activated_skill_ids() -> None:
    result = LoadSkillTool().run({"skill_id": "visual-context"})

    assert result.success is False
    assert result.error == "skill_not_found"


def test_runtime_expands_catalog_after_load_and_restores_it_next_turn() -> None:
    adapter = _ScriptedChatAdapter(
        [
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="load-travel",
                        name="load_skill",
                        arguments={"skill_id": "travel-tool-orchestration"},
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="tool_calls",
                tool_calls=[
                    NativeToolCall(
                        id="search-lodging",
                        name="lodging_search",
                        arguments={
                            "destination": "上海",
                            "check_in": "2026-08-20",
                            "check_out": "2026-08-21",
                        },
                    )
                ],
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="first turn complete",
            ),
            ChatResult(
                provider="scripted",
                model="scripted-model",
                finish_reason="stop",
                response_text="second turn complete",
            ),
        ]
    )
    store = InMemorySessionStore()
    runtime = AgentGraphRuntime(
        registry=create_default_registry(),
        config=ProviderConfig(langgraph_checkpointer_backend="none"),
        chat_adapter=adapter,
        session_store=store,
    )

    try:
        first = runtime.run_state(
            UserRequest(
                user_id="owner-a",
                session_id="session-a",
                text="first sentinel request",
            )
        )
        second = runtime.run_state(
            UserRequest(
                user_id="owner-a",
                session_id="session-a",
                text="follow-up sentinel request",
            )
        )
    finally:
        runtime.close()

    assert first.status == "completed"
    assert second.status == "completed"
    load_result = next(
        result for result in first.tool_results if result.tool_name == "load_skill"
    )
    assert load_result.data is not None
    assert load_result.data["granted_tools"] == ["lodging_search"]
    assert load_result.data["unavailable_tools"] == [
        "mcp.amap_maps.maps_geo",
        "mcp.amap_maps.maps_ip_location",
        "mcp.amap_maps.maps_weather",
        "mcp.amap_maps.maps_bicycling",
        "mcp.amap_maps.maps_direction_walking",
        "mcp.amap_maps.maps_direction_driving",
        "mcp.amap_maps.maps_direction_transit_integrated",
        "mcp.amap_maps.maps_text_search",
        "mcp.amap_maps.maps_around_search",
    ]
    assert load_result.model_observation is not None
    assert load_result.model_observation["granted_tools"] == ["lodging_search"]
    assert load_result.model_observation["unavailable_tools"] == (
        load_result.data["unavailable_tools"]
    )
    assert "lodging_search" not in _request_tool_names(adapter.requests[0])
    assert "lodging_search" in _request_tool_names(adapter.requests[1])
    assert "lodging_search" in _request_tool_names(adapter.requests[2])
    assert "lodging_search" in _request_tool_names(adapter.requests[3])
    assert any(
        result.tool_name == "lodging_search" and result.success
        for result in first.tool_results
    )
    assert second.session_restored_grant_ids == [
        "skill:travel-tool-orchestration"
    ]
