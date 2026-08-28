from __future__ import annotations

from contextlib import nullcontext
from importlib import import_module
import json
from pathlib import Path

import pytest

from scripts import run_server


def _load_graph():
    try:
        return import_module("showcases.studio_evolution.graph").graph
    except ModuleNotFoundError as exc:
        raise AssertionError("Studio showcase graph is missing") from exc


def test_showcase_exposes_only_the_requested_route_topology() -> None:
    graph = _load_graph().get_graph()

    assert set(graph.nodes) == {
        "__start__",
        "execute_route",
        "fast",
        "plan",
        "code",
        "__end__",
    }
    assert {(edge.source, edge.target) for edge in graph.edges} == {
        ("__start__", "execute_route"),
        ("execute_route", "fast"),
        ("execute_route", "plan"),
        ("execute_route", "code"),
        ("fast", "__end__"),
        ("plan", "__end__"),
        ("code", "__end__"),
    }


@pytest.mark.parametrize(
    ("input_state", "expected_branch", "expected_state"),
    [
        ({}, "fast", None),
        ({"route": "fast"}, "fast", {"route": "fast"}),
        ({"route": "plan"}, "plan", {"route": "plan"}),
        ({"route": "code"}, "code", {"route": "code"}),
    ],
)
def test_showcase_routes_without_changing_state(
    input_state: dict[str, str],
    expected_branch: str,
    expected_state: dict[str, str] | None,
) -> None:
    graph = _load_graph()

    updates = list(graph.stream(input_state, stream_mode="updates"))

    assert [next(iter(update)) for update in updates] == [
        "execute_route",
        expected_branch,
    ]
    assert graph.invoke(input_state) == expected_state


def test_showcase_uses_the_single_instance_server_wrapper(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(run_server, "hold_dev_server_lock", lambda: nullcontext())
    monkeypatch.setattr(run_server, "require_available_port", lambda *_args: None)

    def capture_command(command, **_kwargs):
        captured_command = list(command)
        captured["command"] = captured_command
        config_index = captured_command.index("--config")
        config_path = Path(captured_command[config_index + 1])
        captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
        return 0

    monkeypatch.setattr(run_server, "run_command_with_log", capture_command)

    assert run_server.main(
        [
            "--backend",
            "dev",
            "--config",
            "langgraph.showcase.json",
            "--no-env-file",
        ]
    ) == 0
    assert captured["config"]["graphs"] == {
        "studio-evolution-showcase": "./showcases/studio_evolution/graph.py:graph"
    }
    assert captured["config"]["env"] == {}
