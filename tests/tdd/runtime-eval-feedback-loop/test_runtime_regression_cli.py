from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import evals.runtime_regression.cli as runtime_cli


class _Client:
    def __init__(self) -> None:
        self.shutdown_called = False
        self.dataset = SimpleNamespace(
            items=[
                SimpleNamespace(
                    id="ui-item-1",
                    status="ACTIVE",
                    input={
                        "role": "user",
                        "content": "来自 Langfuse UI 的案例",
                        "truncated": False,
                    },
                )
            ]
        )

    def get_dataset(self, name):
        assert name == "assistant-agent-runtime-regressions"
        return self.dataset

    def shutdown(self) -> None:
        self.shutdown_called = True


class _ProviderConfig:
    provider_mode = "real"

    def validate_provider_mode(self) -> None:
        return None

    def resolved_chat_provider(self):
        return SimpleNamespace(model="production-model")


def test_preflight_validates_langfuse_items_and_real_provider_without_running(
    monkeypatch,
    capsys,
) -> None:
    client = _Client()
    monkeypatch.setattr(runtime_cli, "_langfuse_client", lambda: client)
    monkeypatch.setattr(
        runtime_cli.ProviderConfig,
        "from_env",
        staticmethod(lambda: _ProviderConfig()),
    )

    exit_code = runtime_cli.main(
        [
            "--preflight",
            "--no-env-file",
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]
    )

    assert exit_code == 0
    assert client.shutdown_called is True
    assert json.loads(capsys.readouterr().out) == {
        "action": "preflight",
        "status": "ready",
        "dataset_name": "assistant-agent-runtime-regressions",
        "active_item_count": 1,
        "model": "production-model",
    }


def test_removed_local_promotion_action_is_rejected_before_langfuse_access(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_cli,
        "_langfuse_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not access Langfuse")),
    )

    with pytest.raises(SystemExit) as raised:
        runtime_cli.main(["--promote-score", "--no-env-file"])

    assert raised.value.code == 2
