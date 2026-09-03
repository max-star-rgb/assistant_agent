import importlib.util

import pytest

from assistant_agent.config import VisionConfig
from assistant_agent.media.embedding import observability
from assistant_agent.media.video.semantic_store import SessionVisualSemanticStore
from assistant_agent.media.video.token_budget import ContextWindowPolicy
from assistant_agent.media.video.visual_timeline_context import VisualTimelineContextService


@pytest.mark.parametrize(
    "module_name",
    ["visual_context", "visual_context_compactor", "visual_context_models"],
)
def test_retired_visual_context_module_is_absent(module_name: str) -> None:
    assert importlib.util.find_spec(f"assistant_agent.media.video.{module_name}") is None


def test_retired_summary_state_and_config_are_absent() -> None:
    assert not hasattr(SessionVisualSemanticStore, "visual_context_snapshot")
    assert not hasattr(SessionVisualSemanticStore, "visual_context_for_compilation")
    assert not hasattr(SessionVisualSemanticStore, "replace_visual_context_summary")
    assert "visual_context_keep_recent_records" not in VisionConfig.__dataclass_fields__
    assert "visual_context_instruction_reserve_tokens" not in VisionConfig.__dataclass_fields__
    assert "visual_context_image_reserve_tokens" not in VisionConfig.__dataclass_fields__
    assert "visual_context_output_reserve_tokens" not in VisionConfig.__dataclass_fields__


def test_retired_observation_surface_is_absent() -> None:
    assert not hasattr(observability, "VisualContextTraceEvent")
    assert not hasattr(observability, "emit_visual_context_observation")
    assert not hasattr(observability, "visual_context_trace_payload")


def test_visual_timeline_budget_surface_remains() -> None:
    assert VisualTimelineContextService is not None
    assert ContextWindowPolicy(input_token_limit=100).evaluate(70).triggered is True
