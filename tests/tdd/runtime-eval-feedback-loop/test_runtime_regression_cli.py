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
                    expected_output={
                        "role": "assistant",
                        "content": "原始失败回答",
                    },
                )
            ]
        )

    def get_dataset(self, name):
        assert name == "assistant-agent-runtime-regressions"
        return self.dataset

    def shutdown(self) -> None:
        self.shutdown_called = True

    def flush(self) -> None:
        return None


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


def test_runtime_regression_cli_builds_items_through_experiment_runtime_host(
    monkeypatch,
) -> None:
    assert hasattr(runtime_cli, "_create_item_runtime")
    captured = {}

    class Runtime:
        def __init__(self, *, config, trace_store) -> None:
            captured["config"] = config
            captured["trace_store"] = trace_store

    def create_host(builder, *, trace_store_factory):
        captured["trace_store_factory"] = trace_store_factory
        captured["runtime"] = builder("trace-store-sentinel")
        return "host-sentinel"

    monkeypatch.setattr(runtime_cli, "AgentGraphRuntime", Runtime)
    monkeypatch.setattr(runtime_cli, "create_experiment_runtime_host", create_host)
    config = _ProviderConfig()

    assert runtime_cli._create_item_runtime(config) == "host-sentinel"
    assert captured["config"] is config
    assert captured["trace_store"] == "trace-store-sentinel"
    assert isinstance(captured["runtime"], Runtime)
    assert captured["trace_store_factory"] is runtime_cli.create_langfuse_experiment_trace_store


def test_runtime_regression_run_requires_nested_trace_before_success(
    monkeypatch,
    capsys,
) -> None:
    client = _Client()
    calls = []
    monkeypatch.setattr(runtime_cli, "_langfuse_client", lambda: client)
    monkeypatch.setattr(
        runtime_cli.ProviderConfig,
        "from_env",
        staticmethod(lambda: _ProviderConfig()),
    )
    monkeypatch.setattr(
        runtime_cli,
        "run_runtime_regression_experiment",
        lambda client, settings: SimpleNamespace(
            run_name=settings.run_name,
            dataset_run_id="experiment-1",
            dataset_run_url="http://langfuse/run/1",
            dataset_item_ids=("ui-item-1",),
        ),
    )
    monkeypatch.setattr(
        runtime_cli,
        "wait_for_runtime_regression_scores",
        lambda *args, **kwargs: calls.append("scores") or {"ui-item-1": {}},
    )
    assert hasattr(runtime_cli, "wait_for_runtime_regression_trace_completeness")
    monkeypatch.setattr(
        runtime_cli,
        "wait_for_runtime_regression_trace_completeness",
        lambda *args, **kwargs: calls.append("trace") or {"ui-item-1": "1" * 32},
    )

    assert (
        runtime_cli.main(
            [
                "--run",
                "--run-name",
                "run-1",
                "--no-env-file",
                "--allow-real-provider",
                "--allow-runtime-side-effects",
            ]
        )
        == 0
    )
    assert calls == ["scores", "trace"]
    assert json.loads(capsys.readouterr().out)["dataset_run_id"] == "experiment-1"
