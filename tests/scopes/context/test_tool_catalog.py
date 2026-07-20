from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant_agent.schemas.requests import UserRequest
from assistant_agent.schemas.python_interpreter import PYTHON_INTERPRETER_ENABLED_ENV
from assistant_agent.schemas.tools import (
    ToolExecutionPolicy,
    RunToolSet,
    ToolSideEffectPolicy,
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
from assistant_agent.services.tool_policy import ToolPolicyInterpreter
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
    assert transport_only.run_tool_set.qualified_tool_names == ["web_search"]
    assert transport_only.run_tool_set.excluded_reasons == {
        "video_understanding": ["entry_profile_not_exposed"]
    }


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


def test_tool_catalog_exposes_read_and_configured_memory_write_for_agent_service() -> None:
    specs = [
        _agent_service_tool_spec("web_search"),
        _agent_service_tool_spec("shopping_search"),
        _agent_service_tool_spec("memory_retrieval"),
        _write_tool_spec("memory_save"),
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
    assert selection.run_tool_set.excluded_reasons == {}


def test_agent_service_exposure_uses_tool_category_not_tool_name() -> None:
    specs = [
        _read_tool_spec("custom.current_weather"),
        _write_tool_spec("custom.private_billing"),
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
        "custom.private_billing": ["write_not_enabled_by_visibility"]
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


def test_default_registry_exposes_read_generate_and_memory_write_for_agent_service() -> None:
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="早上好，帮我说下今天出门前要注意什么",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )
    selection = select_prompt_tool_specs(request, create_default_registry().list_specs())

    assert "weather" in selection.run_tool_set.exposed_tool_names
    assert "calendar_search" in selection.run_tool_set.exposed_tool_names
    assert "contacts_search" in selection.run_tool_set.exposed_tool_names
    assert "image_generation" in selection.run_tool_set.exposed_tool_names
    assert "memory_media_ingest" in selection.run_tool_set.exposed_tool_names
    assert "memory_retrieval" in selection.run_tool_set.exposed_tool_names
    assert "memory_save" in selection.run_tool_set.exposed_tool_names
    assert "render_3d" in selection.run_tool_set.exposed_tool_names
    assert "web_search" in selection.run_tool_set.exposed_tool_names
    assert "calendar_create" not in selection.run_tool_set.exposed_tool_names
    assert "reminder_create" not in selection.run_tool_set.exposed_tool_names
    assert "weather" not in selection.run_tool_set.excluded_reasons
    assert "calendar_search" not in selection.run_tool_set.excluded_reasons
    assert "contacts_search" not in selection.run_tool_set.excluded_reasons
    assert selection.run_tool_set.excluded_reasons["calendar_create"] == [
        "write_not_enabled_by_visibility"
    ]
    assert selection.run_tool_set.excluded_reasons["reminder_create"] == [
        "write_not_enabled_by_visibility"
    ]


def test_agent_service_text_does_not_drive_visibility_beyond_code_config() -> None:
    specs = [
        _read_tool_spec("weather"),
        _generate_tool_spec("image_generation"),
        _generate_tool_spec("render_3d"),
        _write_tool_spec("memory_save"),
        _write_tool_spec("calendar_create"),
        _dangerous_tool_spec("python_interpreter"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="请生成一张海报，并记住我喜欢冷色调",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == [
        "weather",
        "image_generation",
        "render_3d",
        "memory_save",
    ]
    assert selection.run_tool_set.excluded_reasons["calendar_create"] == [
        "write_not_enabled_by_visibility"
    ]
    assert selection.run_tool_set.excluded_reasons["python_interpreter"] == [
        "dangerous_not_explicitly_enabled"
    ]


def test_agent_service_generate_visibility_is_not_text_triggered() -> None:
    specs = [
        _read_tool_spec("weather"),
        _generate_tool_spec("image_generation"),
        _generate_tool_spec("render_3d"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="把浅灰色沙发放到北欧风客厅看看",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )
    unrelated_request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="随便聊聊",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selection = select_prompt_tool_specs(request, specs)
    unrelated_selection = select_prompt_tool_specs(unrelated_request, specs)

    assert selection.run_tool_set.exposed_tool_names == [
        "weather",
        "image_generation",
        "render_3d",
    ]
    assert unrelated_selection.run_tool_set.exposed_tool_names == (
        selection.run_tool_set.exposed_tool_names
    )
    assert selection.run_tool_set.excluded_reasons == {}


def test_agent_service_capability_text_does_not_expose_write_or_dangerous() -> None:
    specs = [
        _generate_tool_spec("image_generation"),
        _generate_tool_spec("render_3d"),
        _write_tool_spec("memory_save"),
        _write_tool_spec("calendar_create"),
        _dangerous_tool_spec("python_interpreter"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="这轮请使用生成能力，并启用记忆能力",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == [
        "image_generation",
        "render_3d",
        "memory_save",
    ]
    assert selection.run_tool_set.excluded_reasons == {
        "calendar_create": ["write_not_enabled_by_visibility"],
        "python_interpreter": ["dangerous_not_explicitly_enabled"],
    }


def test_agent_service_request_text_does_not_expose_non_memory_write_tools() -> None:
    specs = [
        _write_tool_spec("calendar_create"),
        _write_tool_spec("reminder_create"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="帮我把明天十点会议加到日历，并设置提醒",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == []
    assert selection.run_tool_set.excluded_reasons == {
        "calendar_create": ["write_not_enabled_by_visibility"],
        "reminder_create": ["write_not_enabled_by_visibility"],
    }


def test_agent_service_configured_visibility_exposes_generate_and_write() -> None:
    specs = [
        _generate_tool_spec("render_3d"),
        _write_tool_spec("reminder_create"),
        _write_tool_spec("calendar_create"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="处理一下",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
            "tool_visibility": {
                "configured_tools": ["render_3d", "reminder_create"],
            },
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == ["render_3d", "reminder_create"]
    assert selection.run_tool_set.excluded_reasons["calendar_create"] == [
        "write_not_enabled_by_visibility"
    ]


def test_agent_service_structured_visibility_exposes_generate_and_write() -> None:
    specs = [
        _generate_tool_spec("render_3d"),
        _write_tool_spec("reminder_create"),
        _write_tool_spec("calendar_create"),
    ]
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="处理一下",
        metadata={
            "transport": "agent_service_websocket",
            "gateway": {"session_config": {"entry_profile": "agent_service"}},
            "tool_visibility": {
                "enabled_tools": ["render_3d", "reminder_create"],
            },
        },
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == ["render_3d", "reminder_create"]
    assert selection.run_tool_set.excluded_reasons["calendar_create"] == [
        "write_not_enabled_by_visibility"
    ]


def test_tool_catalog_exposes_default_qualified_tools_independent_of_request_text(monkeypatch) -> None:
    monkeypatch.delenv(PYTHON_INTERPRETER_ENABLED_ENV, raising=False)
    specs = create_default_registry().list_specs()
    requests = [
        UserRequest(user_id="u1", session_id="s1", text="帮我找耳机"),
        UserRequest(user_id="u1", session_id="s1", text="写一段文案"),
        UserRequest(user_id="u1", session_id="s1", text="Momentum 4 值不值得入"),
    ]

    selections = [select_prompt_tool_specs(request, specs) for request in requests]

    expected = [
        "calendar_search",
        "contacts_search",
        "image_generation",
        "memory_ingest_status",
        "memory_media_ingest",
        "memory_retrieval",
        "memory_save",
        "render_3d",
        "shopping_search",
        "tool_search",
        "weather",
        "web_fetch",
        "web_search",
    ]
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
    assert all(
        item.run_tool_set.excluded_reasons["python_interpreter"]
        == [f"missing_required_env:{PYTHON_INTERPRETER_ENABLED_ENV}"]
        for item in selections
    )


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
            "ready_tool_names": ["shopping_search"],
        },
    )
    specs = [
        ToolSpec(name="web_search"),
        ToolSpec(name="task_plan_submit"),
        ToolSpec(name="shopping_search"),
    ]

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.exposed_tool_names == [
        "task_plan_submit",
        "shopping_search",
    ]


def test_qualification_exposes_read_and_generate_but_requires_enabled_write() -> None:
    specs = [
        ToolSpec(name="read", policy=ToolPolicyMetadata(risk="local_read")),
        ToolSpec(name="artifact", policy=ToolPolicyMetadata(risk="transactional")),
        ToolSpec(name="write", policy=ToolPolicyMetadata(risk="external_write")),
    ]
    request = UserRequest(
        user_id="u1", session_id="s1", text="does not classify tools"
    )

    selection = select_prompt_tool_specs(request, specs)

    assert selection.run_tool_set.qualified_tool_names == ["read", "artifact"]
    assert selection.run_tool_set.exposed_tool_names == ["read", "artifact"]
    assert selection.run_tool_set.executable_tool_names == ["read", "artifact"]
    assert selection.run_tool_set.excluded_reasons == {
        "write": ["write_not_enabled_by_visibility"],
    }

    explicit = select_prompt_tool_specs(
        UserRequest(
            user_id="u1",
            session_id="s1",
            text="does not classify tools",
            metadata={"tool_visibility": {"enabled_tools": ["artifact", "write"]}},
        ),
        specs,
    )

    assert explicit.run_tool_set.qualified_tool_names == ["read", "artifact", "write"]


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


def test_identity_recall_exposes_memory_read_and_configured_memory_write() -> None:
    specs = create_default_registry().list_specs()

    selection = select_prompt_tool_specs(
        UserRequest(
            user_id="u1", session_id="s1", text="生成一段商品偏好小红书乐事薯片文案"
        ),
        specs,
    )

    names = [spec.name for spec in selection.prompt_tool_specs]
    assert "memory_retrieval" in names
    assert "memory_media_ingest" in names
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
    assert any(
        "retryable transient failures once" in item
        for item in descriptor.runtime_constraints
    )
    assert descriptor.permissions == ["tool:web_search"]


def test_capability_catalog_auto_recalls_realtime_web_search_descriptor() -> None:
    specs = create_default_registry().list_specs()
    request = UserRequest(
        user_id="u1",
        session_id="s1",
        text="查一下今天 AI 行业最新消息",
    )
    tool_selection = select_prompt_tool_specs(request, specs)
    original_run_tool_set = tool_selection.run_tool_set.model_dump(mode="json")
    governed_spec = next(spec for spec in tool_selection.prompt_tool_specs if spec.name == "web_search")
    original_retry_count = ToolPolicyInterpreter().view_for_spec(governed_spec).retry_count

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
    assert tool_selection.run_tool_set.model_dump(mode="json") == original_run_tool_set
    assert ToolPolicyInterpreter().view_for_spec(governed_spec).retry_count == original_retry_count


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
    public_spec = _read_tool_spec("web_search")
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
- shopping_search

## Permissions
- tool:shopping_search
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
        ToolSpec(name="shopping_search", required_inputs=["query"]),
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
    return _read_tool_spec(
        name,
        allowed_entry_profiles=["agent_service"],
        requires_media=requires_media or [],
    )


def _read_tool_spec(
    name: str,
    *,
    allowed_entry_profiles: list[str] | None = None,
    requires_media: list[str] | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        side_effect=ToolSideEffectPolicy(
            level="external_read",
            requires_confirmation=False,
        ),
        execution=ToolExecutionPolicy(
            dependency_mode="independent",
            realtime_safety="safe",
            artifact_reuse="reusable",
        ),
        visibility=VisibilityPolicy(
            allowed_entry_profiles=allowed_entry_profiles or [],
            requires_media=requires_media or [],
        ),
    )


def _generate_tool_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        side_effect=ToolSideEffectPolicy(
            level="compensatable",
            requires_confirmation=False,
        ),
        execution=ToolExecutionPolicy(
            dependency_mode="terminal",
            resource_writes=["artifact"],
            realtime_safety="needs_progress",
            artifact_reuse="requires_validation",
        ),
    )


def _write_tool_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        side_effect=ToolSideEffectPolicy(
            level="pending_confirmation",
            requires_confirmation=True,
        ),
        execution=ToolExecutionPolicy(
            dependency_mode="terminal",
            resource_writes=["user_state"],
            realtime_safety="needs_confirmation",
            artifact_reuse="do_not_reuse",
        ),
    )


def _dangerous_tool_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        side_effect=ToolSideEffectPolicy(
            level="local_read",
            requires_confirmation=False,
        ),
        execution=ToolExecutionPolicy(
            dependency_mode="requires_prior_observation",
            realtime_safety="unsafe",
            artifact_reuse="requires_validation",
        ),
    )
