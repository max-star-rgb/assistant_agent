from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.tools import (
    RunToolSet,
    ToolPolicyMetadata,
    ToolSpec,
    VisibilityPolicy,
)
from assistant_agent.services.context.capability_catalog import (
    select_tool_capability_descriptors,
)
from assistant_agent.services.context.skill_loader import load_repo_skill_descriptors
from assistant_agent.services.context import tool_catalog
from assistant_agent.services.context.tool_catalog import select_prompt_tool_specs
from assistant_agent.services.agent_service_entry import is_trusted_agent_service_request
from assistant_agent.tools.registry import create_default_registry


def test_trusted_agent_service_predicate_rejects_transport_only() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="眼前是什么？",
        metadata={"transport": "agent_service_websocket"},
    )

    assert is_trusted_agent_service_request(request) is False


def test_trusted_agent_service_predicate_rejects_profile_only() -> None:
    metadata = {
        "gateway": {"session_config": {"entry_profile": "agent_service"}},
    }

    assert is_trusted_agent_service_request(metadata) is False


def test_tool_catalog_uses_trusted_agent_service_entry_to_narrow_tools() -> None:
    specs = [
        _agent_service_tool_spec("web_search"),
        _agent_service_tool_spec("video_understanding", requires_media=["video"]),
    ]
    trusted_request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="眼前是什么？",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )
    transport_only_request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="分析视频",
        metadata={"transport": "agent_service_websocket"},
    )

    trusted = select_prompt_tool_specs(trusted_request, specs)
    transport_only = select_prompt_tool_specs(transport_only_request, specs)

    assert trusted.run_tool_set.qualified_tool_names == ["web_search"]
    assert trusted.run_tool_set.excluded_reasons == {
        "video_understanding": ["entry_profile_not_exposed"]
    }
    assert transport_only.run_tool_set.qualified_tool_names == [
        "web_search",
        "video_understanding",
    ]


def test_tool_catalog_exposes_video_understanding_for_agent_service_when_video_is_active() -> None:
    specs = [
        _agent_service_tool_spec("web_search"),
        _agent_service_tool_spec("video_understanding", requires_media=["video"]),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="屏幕里面有什么？",
        video_ids=["agent-service-video"],
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selected = select_prompt_tool_specs(request, specs)

    assert selected.run_tool_set.qualified_tool_names == [
        "web_search",
        "video_understanding",
    ]
    assert selected.run_tool_set.executable_tool_names == [
        "web_search",
        "video_understanding",
    ]
    assert selected.run_tool_set.excluded_reasons == {}


def test_tool_catalog_agent_service_video_exposure_uses_structured_media_not_text() -> None:
    specs = [
        _agent_service_tool_spec("web_search"),
        _agent_service_tool_spec("video_understanding", requires_media=["video"]),
    ]
    no_video_request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="眼前是什么？",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )
    active_video_request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="你好",
        video_ids=["agent-service-video"],
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    without_video = select_prompt_tool_specs(no_video_request, specs)
    with_video = select_prompt_tool_specs(active_video_request, specs)

    assert without_video.run_tool_set.qualified_tool_names == ["web_search"]
    assert without_video.run_tool_set.excluded_reasons == {
        "video_understanding": ["entry_profile_not_exposed"]
    }
    assert with_video.run_tool_set.qualified_tool_names == [
        "web_search",
        "video_understanding",
    ]


def test_tool_catalog_exposes_unified_shopping_tool_for_agent_service() -> None:
    specs = [
        _agent_service_tool_spec("web_search"),
        _agent_service_tool_spec("shopping_search"),
        ToolSpec(name="product_search"),
        ToolSpec(name="price_compare"),
        _agent_service_tool_spec("memory_retrieval"),
        _agent_service_tool_spec("memory_save"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="帮我买个划算的耳机",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.qualified_tool_names == [
        "web_search",
        "shopping_search",
        "memory_retrieval",
        "memory_save",
    ]
    assert selection.run_tool_set.executable_tool_names == [
        "web_search",
        "shopping_search",
        "memory_retrieval",
        "memory_save",
    ]
    assert selection.run_tool_set.excluded_reasons == {
        "product_search": ["entry_profile_not_exposed"],
        "price_compare": ["entry_profile_not_exposed"],
    }


def test_agent_service_exposure_uses_visibility_metadata_not_tool_name() -> None:
    specs = [
        _agent_service_tool_spec("custom.current_weather"),
        ToolSpec(name="custom.private_billing"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下天气",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.qualified_tool_names == ["custom.current_weather"]
    assert selection.run_tool_set.excluded_reasons == {
        "custom.private_billing": ["entry_profile_not_exposed"]
    }


def test_agent_service_visibility_requires_declared_active_media() -> None:
    specs = [
        _agent_service_tool_spec("custom.live_camera", requires_media=["video"]),
    ]
    metadata = {
        "transport": "agent_service_websocket",
        "gateway": {"session_config": {"entry_profile": "agent_service"}},
    }

    no_video = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="眼前是什么？", metadata=metadata),
        specs,
    )
    with_video = select_prompt_tool_specs(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="你好",
            video_ids=["agent-service-video"],
            metadata=metadata,
        ),
        specs,
    )

    assert no_video.run_tool_set.qualified_tool_names == []
    assert no_video.run_tool_set.excluded_reasons == {
        "custom.live_camera": ["entry_profile_not_exposed"]
    }
    assert with_video.run_tool_set.qualified_tool_names == ["custom.live_camera"]
    assert with_video.run_tool_set.excluded_reasons == {}


def test_default_registry_declares_agent_service_visibility_metadata() -> None:
    specs = {spec.name: spec for spec in create_default_registry().list_specs()}

    for tool_name in {
        "web_search",
        "shopping_search",
        "memory_retrieval",
        "memory_save",
    }:
        assert specs[tool_name].visibility.allowed_entry_profiles == ["agent_service"]
        assert specs[tool_name].visibility.requires_media == []

    video = specs["video_understanding"]
    assert video.visibility.allowed_entry_profiles == ["agent_service"]
    assert video.visibility.requires_media == ["video"]
    assert specs["product_search"].visibility.allowed_entry_profiles == []
    assert specs["price_compare"].visibility.allowed_entry_profiles == []


def test_tool_catalog_exposes_all_qualified_tools_independent_of_request_text() -> None:
    specs = create_default_registry().list_specs()
    requests = [
        UserRequest(user_id="u1", session_id="s1", text="帮我找耳机"),
        UserRequest(user_id="u1", session_id="s1", text="写一段文案"),
        UserRequest(user_id="u1", session_id="s1", text="Momentum 4 值不值得入"),
    ]

    selections = [select_prompt_tool_specs(request, specs) for request in requests]

    expected = [spec.name for spec in specs]
    assert [[spec.name for spec in item.qualified_tool_specs] for item in selections] == [
        expected,
        expected,
        expected,
    ]
    assert [[spec.name for spec in item.prompt_tool_specs] for item in selections] == [
        expected,
        expected,
        expected,
    ]
    assert all(item.run_tool_set.executable_tool_names == expected for item in selections)
    assert all(item.summary.selection_reasons == ["recall_identity"] for item in selections)


def test_identity_recall_preserves_qualified_tool_order() -> None:
    request = UserRequest(user_id="u1", session_id="s1", text="arbitrary text")
    specs = [ToolSpec(name="third"), ToolSpec(name="first"), ToolSpec(name="second")]

    recall = getattr(tool_catalog, "recall_qualified_tool_specs", None)

    assert recall is not None
    recalled = recall(request, specs)

    assert recalled == specs
    assert recalled is not specs


def test_trusted_durable_resume_exposes_only_ready_tools_and_plan_revision() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="resume",
        metadata={
            "_trusted_durable_execution": True,
            "ready_tool_names": ["product_search"],
        },
    )
    specs = [
        ToolSpec(name="web_search"),
        ToolSpec(name="task_plan_submit"),
        ToolSpec(name="product_search"),
    ]

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == [
        "task_plan_submit",
        "product_search",
    ]


def test_qualification_keeps_all_risk_levels_visible() -> None:
    specs = [
        ToolSpec(name="read", policy=ToolPolicyMetadata(risk="local_read")),
        ToolSpec(name="artifact", policy=ToolPolicyMetadata(risk="transactional")),
        ToolSpec(name="write", policy=ToolPolicyMetadata(risk="external_write")),
    ]
    request = UserRequest(
        user_id="u1", session_id="s1", text="does not classify tools"
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.qualified_tool_names == [
        "read",
        "artifact",
        "write",
    ]
    assert selection.run_tool_set.exposed_tool_names == [
        "read",
        "artifact",
        "write",
    ]
    assert selection.run_tool_set.executable_tool_names == [
        "read",
        "artifact",
        "write",
    ]


def test_run_tool_set_rejects_exposed_tool_outside_qualified_set() -> None:
    with pytest.raises(ValidationError, match="exposed_tool_names"):
        RunToolSet(
            registered_tool_names=["registered"],
            qualified_tool_names=["registered"],
            exposed_tool_names=["hidden"],
        )


def test_tool_catalog_excludes_tool_when_required_environment_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("ASSISTANT_AGENT_TEST_TOOL_KEY", raising=False)
    spec = ToolSpec(
        name="private.lookup",
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(
                requires_env=["ASSISTANT_AGENT_TEST_TOOL_KEY"],
            )
        ),
    )

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="private lookup"),
        [spec],
    )

    assert selection.qualified_tool_specs == []
    assert selection.prompt_tool_specs == []
    assert selection.run_tool_set.qualified_tool_names == []
    assert selection.run_tool_set.excluded_reasons == {
        "private.lookup": ["missing_required_env:ASSISTANT_AGENT_TEST_TOOL_KEY"]
    }


def test_tool_catalog_requires_explicit_enable_for_disabled_tool() -> None:
    spec = ToolSpec(
        name="weather.lookup",
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(
                toolset="personal.readonly",
                enabled_by_default=False,
            )
        ),
    )
    hidden = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="weather lookup"),
        [spec],
    )
    enabled = select_prompt_tool_specs(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="weather lookup",
            metadata={
                "tool_visibility": {
                    "enabled_toolsets": ["personal.readonly"],
                }
            },
        ),
        [spec],
    )

    assert hidden.prompt_tool_specs == []
    assert hidden.run_tool_set.excluded_reasons == {
        "weather.lookup": ["disabled_by_default"]
    }
    assert [item.name for item in enabled.prompt_tool_specs] == ["weather.lookup"]
    assert enabled.run_tool_set.executable_tool_names == ["weather.lookup"]


def test_request_text_does_not_qualify_skill_only_tool(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "private_search",
        """
---
name: private_search
description: Private search guidance.
---
## Governed Tools
- private.lookup

## Permissions
- tool:private.lookup

## Visibility
- tags: private-lookup
""",
    )
    catalog = load_repo_skill_descriptors(tmp_path)
    spec = ToolSpec(
        name="private.lookup",
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(
                enabled_by_default=False,
                skill_only=True,
            )
        ),
    )

    selection = select_prompt_tool_specs(
        UserRequest(user_id="u1", session_id="s1", text="private-lookup"),
        [spec],
        skill_catalog=catalog,
    )

    assert selection.active_skill_ids == []
    assert selection.qualified_tool_specs == []
    assert selection.run_tool_set.excluded_reasons == {
        "private.lookup": ["skill_activation_required"]
    }


def test_explicit_enabled_skill_qualifies_skill_only_tool(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "private_search",
        """
---
name: private_search
description: Private search guidance.
---
## Governed Tools
- private.lookup

## Permissions
- tool:private.lookup
""",
    )
    catalog = load_repo_skill_descriptors(tmp_path)
    spec = ToolSpec(
        name="private.lookup",
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(
                enabled_by_default=False,
                skill_only=True,
            )
        ),
    )
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="unclassified text",
        metadata={"tool_visibility": {"enabled_skills": ["private_search"]}},
    )

    selection = select_prompt_tool_specs(
        request,
        [spec],
        skill_catalog=catalog,
    )

    assert selection.active_skill_ids == ["private_search"]
    assert selection.run_tool_set.qualified_tool_names == ["private.lookup"]
    assert selection.prompt_tool_specs == [spec]


def test_identity_recall_exposes_qualified_memory_tools_without_text_routing() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1", session_id="s1", text="生成一段商品偏好小红书乐事薯片文案"
        ),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert "memory_retrieval" in names
    assert "memory_save" in names
    assert selection.summary.selection_reasons == ["recall_identity"]


def test_capability_catalog_selects_realtime_web_search_descriptor() -> None:
    specs = create_default_registry().list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={
            "tool_visibility": {"enabled_skills": ["realtime_web_search"]}
        },
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
    )

    assert [item.name for item in capability_selection.capabilities] == ["realtime_web_search"]
    descriptor = capability_selection.capabilities[0]
    assert descriptor.governed_tools == ["web_search"]
    assert descriptor.required_inputs_by_tool == {"web_search": ["query"]}
    assert any("ToolExecutor" in item for item in descriptor.runtime_constraints)


def test_capability_catalog_auto_recalls_realtime_web_search_descriptor() -> None:
    specs = create_default_registry().list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=tool_selection.qualified_tool_specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
    )

    assert [item.name for item in capability_selection.capabilities] == ["realtime_web_search"]
    assert capability_selection.skill_report.explicit_skill_ids == []
    assert capability_selection.skill_report.auto_candidate_skill_ids == ["realtime_web_search"]
    assert "realtime_web_search" in capability_selection.skill_report.auto_recall_reasons
    assert any(
        reason == "capability_catalog_auto_recalled:realtime_web_search"
        for reason in capability_selection.selection_reasons
    )


def test_capability_catalog_omits_descriptor_when_governed_tool_missing() -> None:
    specs = [spec for spec in create_default_registry().list_specs() if spec.name != "web_search"]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={
            "tool_visibility": {"enabled_skills": ["realtime_web_search"]}
        },
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
    )

    assert capability_selection.capabilities == []


def test_capability_catalog_auto_recall_does_not_expose_missing_governed_tool() -> None:
    specs = [spec for spec in create_default_registry().list_specs() if spec.name != "web_search"]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=tool_selection.qualified_tool_specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
    )

    assert capability_selection.capabilities == []
    assert capability_selection.skill_report.auto_candidate_skill_ids == ["realtime_web_search"]
    assert any(
        item.skill_id == "realtime_web_search"
        and item.reason == "governed_tool_unqualified"
        and item.tool_name == "web_search"
        for item in capability_selection.skill_report.skipped
    )


def test_capability_catalog_prefers_repo_skill_over_builtin(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Repo-local search guidance.
---
## Governed Tools
- web_search

## Permissions
- tool:web_search

## Required Inputs
- web_search: query

## When To Use
- Repo guidance for latest information.
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={"tool_visibility": {"enabled_skills": ["realtime_web_search"]}},
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert [item.name for item in capability_selection.capabilities] == ["realtime_web_search"]
    descriptor = capability_selection.capabilities[0]
    assert descriptor.description == "Repo-local search guidance."
    assert descriptor.permissions == ["tool:web_search"]
    assert descriptor.when_to_use == ["Repo guidance for latest information."]
    assert any("ToolExecutor" in item for item in descriptor.runtime_constraints)
    assert "capability_catalog_selected:realtime_web_search" in capability_selection.selection_reasons
    assert capability_selection.skill_report.loaded_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.selected_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.override_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.builtin_fallback_skill_ids == []
    assert capability_selection.skill_report.governed_tool_names == ["web_search"]


def test_auto_recalled_skill_does_not_qualify_skill_only_tool(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "private_search",
        """
---
name: private_search
description: Private lookup guidance.
---
## Governed Tools
- private.lookup

## Permissions
- tool:private.lookup

## When To Use
- User asks for private lookup.
""",
    )
    catalog = load_repo_skill_descriptors(tmp_path)
    private_spec = ToolSpec(
        name="private.lookup",
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(
                enabled_by_default=False,
                skill_only=True,
            )
        ),
    )
    public_spec = ToolSpec(name="web_search", required_inputs=["query"])
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="please do a private lookup",
    )

    tool_selection = select_prompt_tool_specs(
        request,
        [private_spec, public_spec],
        skill_catalog=catalog,
    )
    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=tool_selection.qualified_tool_specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
        skill_catalog=catalog,
    )

    assert tool_selection.run_tool_set.qualified_tool_names == ["web_search"]
    assert tool_selection.run_tool_set.executable_tool_names == ["web_search"]
    assert tool_selection.run_tool_set.excluded_reasons == {
        "private.lookup": ["skill_activation_required"]
    }
    assert capability_selection.capabilities == []
    assert capability_selection.skill_report.auto_candidate_skill_ids == ["private_search"]
    assert any(
        item.skill_id == "private_search"
        and item.reason == "governed_tool_unqualified"
        and item.tool_name == "private.lookup"
        for item in capability_selection.skill_report.skipped
    )


def test_capability_catalog_disabled_repo_skill_suppresses_builtin_fallback(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Disabled local search guidance.
enabled: false
---
## Governed Tools
- web_search

## Permissions
- tool:web_search
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={"tool_visibility": {"enabled_skills": ["realtime_web_search"]}},
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert capability_selection.capabilities == []
    assert capability_selection.skill_report.loaded_skill_ids == []
    assert capability_selection.skill_report.selected_skill_ids == []
    assert capability_selection.skill_report.override_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.builtin_fallback_skill_ids == []
    assert [
        item.model_dump(mode="json") for item in capability_selection.skill_report.skipped
    ] == [
        {
            "skill_id": "realtime_web_search",
            "reason": "skill_disabled",
            "tool_name": None,
            "permission": None,
        }
    ]


def test_capability_catalog_invalid_repo_skill_suppresses_builtin_fallback(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "realtime_web_search",
        """
---
name: realtime_web_search
description: Invalid local search guidance.
---
## Governed Tools
- web_search

## Permissions
- tool:web_search
- shell:run
""",
    )
    specs = create_default_registry().list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={"tool_visibility": {"enabled_skills": ["realtime_web_search"]}},
    )
    tool_selection = select_prompt_tool_specs(request, specs)

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=specs,
        prompt_tool_specs=tool_selection.prompt_tool_specs,
        tool_catalog_summary=tool_selection.summary,
        repo_root=tmp_path,
    )

    assert capability_selection.capabilities == []
    assert capability_selection.skill_report.override_skill_ids == ["realtime_web_search"]
    assert capability_selection.skill_report.builtin_fallback_skill_ids == []
    assert capability_selection.skill_report.permission_issue_count == 1
    assert capability_selection.skill_report.skipped[0].reason == "invalid_permission"


def test_capability_catalog_omits_repo_skill_when_governed_tool_unavailable(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "custom_missing",
        """
---
name: custom_missing
description: This skill points at a tool that is not registered.
---
## Governed Tools
- missing_tool

## Permissions
- tool:missing_tool
""",
    )
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={"tool_visibility": {"enabled_skills": ["custom_missing"]}},
    )
    specs = [ToolSpec(name="web_search", required_inputs=["query"])]

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=specs,
        prompt_tool_specs=specs,
        tool_catalog_summary=select_prompt_tool_specs(request, specs).summary,
        repo_root=tmp_path,
    )

    assert "custom_missing" not in [item.name for item in capability_selection.capabilities]
    assert any(
        reason == "capability_catalog_skipped:custom_missing:governed_tool_unqualified"
        for reason in capability_selection.selection_reasons
    )


def test_capability_catalog_omits_repo_skill_when_governed_tool_not_prompt_selected(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "product_research",
        """
---
name: product_research
description: Product research guidance.
---
## Governed Tools
- product_search

## Permissions
- tool:product_search
""",
    )
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
        metadata={"tool_visibility": {"enabled_skills": ["product_research"]}},
    )
    qualified_specs = [
        ToolSpec(name="web_search", required_inputs=["query"]),
        ToolSpec(name="product_search", required_inputs=["query"]),
    ]
    prompt_specs = [qualified_specs[0]]

    capability_selection = select_tool_capability_descriptors(
        request=request,
        qualified_tool_specs=qualified_specs,
        prompt_tool_specs=prompt_specs,
        tool_catalog_summary=select_prompt_tool_specs(request, qualified_specs).summary,
        repo_root=tmp_path,
    )

    assert "product_research" not in [item.name for item in capability_selection.capabilities]
    assert any(
        reason == "capability_catalog_skipped:product_research:governed_tool_not_prompt_selected"
        for reason in capability_selection.selection_reasons
    )


def _write_skill(root: Path, name: str, content: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")


def _agent_service_tool_spec(
    name: str,
    *,
    requires_media: list[str] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        policy=ToolPolicyMetadata(
            visibility=VisibilityPolicy(
                allowed_entry_profiles=["agent_service"],
                requires_media=requires_media or [],
            ),
        ),
    )
