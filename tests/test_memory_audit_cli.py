from argparse import Namespace

import scripts.memory_audit as memory_audit


def test_memory_audit_cli_builds_list_request(monkeypatch) -> None:
    calls = []

    def fake_request(server, method, path, *, params=None, timeout=10.0):
        calls.append((server, method, path, params, timeout))
        return {"total": 0, "items": []}

    monkeypatch.setattr(memory_audit, "request_json", fake_request)
    args = Namespace(
        command="list",
        server="http://example.test",
        user_id="u 1",
        memory_type="preference",
        include_content=True,
    )

    payload = memory_audit.run_command(args)

    assert payload == {"total": 0, "items": []}
    assert calls == [
        (
            "http://example.test",
            "GET",
            "/memory/users/u%201/items",
            {"include_content": True, "memory_type": "preference"},
            10.0,
        )
    ]


def test_memory_audit_cli_requires_yes_for_delete(capsys) -> None:
    status = memory_audit.main(["delete", "--user-id", "u1", "--memory-id", "m1"])

    assert status == 2
    assert "requires --yes" in capsys.readouterr().err


def test_memory_audit_cli_runs_delete_with_yes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        memory_audit,
        "request_json",
        lambda server, method, path, **kwargs: {"deleted": {"memory_items": 1}},
    )

    status = memory_audit.main(["--json", "delete", "--user-id", "u1", "--memory-id", "m1", "--yes"])

    assert status == 0
    assert '"memory_items": 1' in capsys.readouterr().out
