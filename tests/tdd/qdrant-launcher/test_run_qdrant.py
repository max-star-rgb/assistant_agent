from __future__ import annotations

from pathlib import Path
from threading import Event

from scripts import run_qdrant


def test_supervisor_starts_waits_and_stops_only_qdrant() -> None:
    compose_calls: list[tuple[str, ...]] = []
    stop_requested = Event()
    stop_requested.set()

    result = run_qdrant._supervise_qdrant(
        stop_requested=stop_requested,
        compose_fn=lambda *args, **_kwargs: compose_calls.append(args),
        health_fn=lambda _stop_requested: True,
    )

    assert result == 0
    assert compose_calls == [
        ("up", "-d", "--no-build", "--pull", "never", "qdrant"),
        ("stop", "qdrant"),
    ]


def test_supervisor_stops_qdrant_when_health_never_becomes_ready() -> None:
    compose_calls: list[tuple[str, ...]] = []

    result = run_qdrant._supervise_qdrant(
        stop_requested=Event(),
        compose_fn=lambda *args, **_kwargs: compose_calls.append(args),
        health_fn=lambda _stop_requested: False,
    )

    assert result == 1
    assert compose_calls[-1] == ("stop", "qdrant")


def test_supervisor_stops_qdrant_when_start_is_interrupted() -> None:
    compose_calls: list[tuple[str, ...]] = []

    def interrupt_start(*args: str, **_kwargs: object) -> None:
        compose_calls.append(args)
        if args[0] == "up":
            raise InterruptedError

    result = run_qdrant._supervise_qdrant(
        stop_requested=Event(),
        compose_fn=interrupt_start,
    )

    assert result == 130
    assert compose_calls[-1] == ("stop", "qdrant")


def test_compose_command_uses_visual_memory_profile_and_explicit_file() -> None:
    assert run_qdrant._compose_command("ps", "qdrant") == [
        "docker",
        "compose",
        "-f",
        str(run_qdrant.COMPOSE_FILE),
        "--profile",
        "visual-memory",
        "ps",
        "qdrant",
    ]


def test_main_rejects_missing_compose_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_qdrant, "COMPOSE_FILE", tmp_path / "missing.yaml")

    assert run_qdrant.main() == 2


def test_health_wait_fails_immediately_after_qdrant_exits() -> None:
    health_probes: list[bool] = []

    result = run_qdrant._wait_until_healthy(
        Event(),
        health_probe=lambda: health_probes.append(False) or False,
        service_running=lambda: False,
    )

    assert result is False
    assert health_probes == [False]
