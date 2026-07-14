import json

from scripts.run_proactive_wake_demo import main


def test_proactive_wake_demo_establishes_baseline_then_delivers_one_change(
    tmp_path, capsys
) -> None:
    db_path = tmp_path / "wake.sqlite3"
    expected = {
        "offline": True,
        "llm_calls": 0,
        "probe_calls": 2,
        "baseline_status": "baseline_established",
        "changed_status": "enqueued",
        "delivered_count": 1,
        "delivery_status": "sent",
    }

    for _ in range(2):
        exit_code = main(["--db", str(db_path)])
        payload = json.loads(capsys.readouterr().out)

        assert exit_code == 0
        assert payload == expected
