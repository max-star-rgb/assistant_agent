from __future__ import annotations

from typing import Any

from scripts import agent_cli


class _Threads:
    def create(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "thread_id": "thread-existing",
            "metadata": {"assistant_graph_id": "assistant-native-v1"},
        }


class _Runs:
    def __init__(self) -> None:
        self.wait_calls = 0

    def wait(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.wait_calls += 1
        return {"messages": []}


class _Client:
    def __init__(self) -> None:
        self.threads = _Threads()
        self.runs = _Runs()


def test_cli_thread_id_rejects_existing_v1_thread_before_run(
    monkeypatch,
    capsys,
) -> None:
    """Catches --thread-id silently starting v2 on a legacy thread."""

    client = _Client()
    monkeypatch.setattr(agent_cli, "get_sync_client", lambda **_kwargs: client)
    monkeypatch.setattr(
        "sys.argv",
        ["agent_cli.py", "--thread-id", "thread-existing", "hello"],
    )

    assert agent_cli.main() == 1
    assert client.runs.wait_calls == 0
    assert "thread graph" in capsys.readouterr().out
