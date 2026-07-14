import json

from scripts.run_proactive_wake_demo import main


def test_proactive_wake_demo_establishes_baseline_then_delivers_one_change(
    tmp_path, capsys
) -> None:
    exit_code = main(["--db", str(tmp_path / "wake.sqlite3")])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["offline"] is True
    assert payload["llm_calls"] == 0
    assert payload["probe_calls"] == 2
    assert payload["baseline_status"] == "baseline_established"
    assert payload["changed_status"] == "enqueued"
    assert payload["delivered_count"] == 1
    assert payload["delivery_status"] == "sent"
