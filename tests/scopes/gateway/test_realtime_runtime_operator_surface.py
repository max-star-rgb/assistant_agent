from pathlib import Path


def test_scripts_readme_declares_realtime_entry_layers() -> None:
    readme = Path("scripts/README.md").read_text(encoding="utf-8")

    assert "Primary realtime runtime entries" in readme
    assert "scripts/run_server.py" in readme
    assert "scripts/realtime_media_client.py" in readme
    assert "scripts/run_gateway_client.py" in readme
    assert "scripts/run_realtime_call_simulator.py" in readme
    assert "Not primary product entries" in readme
    assert "scripts/run_assistant_cli.py" in readme
    assert "scripts/run_demo_flows.py" in readme
    assert "scripts/run_evals.py" in readme
    assert "scripts/run_client.py" not in readme
    assert "/ws/agent" not in readme


def test_realtime_runtime_runbook_covers_minimal_closed_loop() -> None:
    runbook = Path("docs/development/realtime-runtime-operator-runbook.md").read_text(
        encoding="utf-8"
    )

    for expected in (
        "scripts/run_server.py --provider mock --image-provider mock",
        "scripts/run_realtime_call_simulator.py --scenario all --quiet",
        "scripts/realtime_media_client.py --server http://127.0.0.1:8000 --scenario all",
        "scripts/realtime_media_client.py --server http://127.0.0.1:8000 --interactive",
        "scripts/run_gateway_client.py --server http://127.0.0.1:8000",
        "GET /traces/{trace_id}",
        "cancelled/interrupted turns must not write durable memory",
    ):
        assert expected in runbook
    assert "/demo/console" not in runbook
    assert "/ws/agent" not in runbook
    assert "scripts/run_client.py" not in runbook
    assert "ASR" in runbook
    assert "TTS" in runbook
