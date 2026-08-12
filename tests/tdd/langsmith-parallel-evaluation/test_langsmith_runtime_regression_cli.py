from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

import evals.langsmith_runtime_regression.cli as cli


EXAMPLE_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")


class _Client:
    def __init__(self) -> None:
        self.flushed = False
        self.closed = False
        self.dataset = SimpleNamespace(id=UUID(int=2))
        self.example = SimpleNamespace(
            id=EXAMPLE_ID,
            inputs={"role": "user", "content": "问题", "truncated": False},
            outputs={"role": "assistant", "content": "失败回答"},
            metadata={"active": True},
        )

    def read_dataset(self, *, dataset_name):
        assert dataset_name == "assistant-agent-runtime-regressions"
        return self.dataset

    def list_examples(self, *, dataset_id):
        return iter([self.example])

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True


class _RealConfig:
    provider_mode = "real"

    def validate_provider_mode(self):
        return None

    def resolved_chat_provider(self):
        return SimpleNamespace(model="production-model")


def test_inspect_never_builds_provider_or_runtime(monkeypatch, capsys) -> None:
    client = _Client()
    monkeypatch.setattr(cli, "_langsmith_client", lambda: client)
    monkeypatch.setattr(
        cli.ProviderConfig,
        "from_env",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("provider"))),
    )

    assert cli.main(["--inspect", "--no-env-file"]) == 0

    assert json.loads(capsys.readouterr().out)["active_example_count"] == 1
    assert client.flushed is True
    assert client.closed is True


def test_preflight_requires_both_operator_flags(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_langsmith_client", _Client)

    with pytest.raises(SystemExit):
        cli.main(
            ["--preflight", "--no-env-file", "--allow-real-provider"]
        )


def test_run_requires_real_provider(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_langsmith_client", _Client)
    monkeypatch.setattr(
        cli.ProviderConfig,
        "from_env",
        staticmethod(lambda: SimpleNamespace(provider_mode="mock")),
    )

    assert cli.main(
        [
            "--run",
            "--run-name",
            "r1",
            "--no-env-file",
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]
    ) == 2
    assert "requires MULTIMODAL_AGENT_PROVIDER_MODE=real" in capsys.readouterr().out


def test_client_initialization_failure_is_controlled_and_sanitized(
    monkeypatch,
    capsys,
) -> None:
    def fail_to_create_client():
        raise RuntimeError("api_key=sk-secret-value /home/private/client.json")

    monkeypatch.setattr(cli, "_langsmith_client", fail_to_create_client)

    assert cli.main(["--inspect", "--no-env-file"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "langsmith_runtime_regression_infrastructure_failure"
    assert "sk-secret-value" not in output["message"]
    assert "/home/private/client.json" not in output["message"]


def test_client_lifecycle_failure_is_controlled_and_closes_client(
    monkeypatch,
    capsys,
) -> None:
    client = _Client()

    def fail_to_flush():
        client.flushed = True
        raise RuntimeError("token=sk-close-secret")

    monkeypatch.setattr(client, "flush", fail_to_flush)
    monkeypatch.setattr(cli, "_langsmith_client", lambda: client)

    assert cli.main(["--inspect", "--no-env-file"]) == 2

    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "langsmith_runtime_regression_infrastructure_failure"
    assert "sk-close-secret" not in output["message"]
    assert client.flushed is True
    assert client.closed is True


def test_item_runtime_is_the_native_runtime_without_otel_binding(monkeypatch) -> None:
    captured = {}

    class Runtime:
        def __init__(self, *, config):
            captured["config"] = config

    monkeypatch.setattr(cli, "AgentGraphRuntime", Runtime)
    config = _RealConfig()

    runtime = cli._create_item_runtime(config)

    assert isinstance(runtime, Runtime)
    assert captured == {"config": config}


def test_cli_owns_one_top_level_asyncio_run(monkeypatch, capsys) -> None:
    client = _Client()
    real_asyncio_run = asyncio.run
    calls = []

    def run_once(coroutine):
        calls.append(coroutine)
        return real_asyncio_run(coroutine)

    monkeypatch.setattr(cli, "_langsmith_client", lambda: client)
    monkeypatch.setattr(cli.asyncio, "run", run_once)

    assert cli.main(["--inspect", "--no-env-file"]) == 0
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["action"] == "inspect"


def test_run_reports_experiment_and_complete_feedback(monkeypatch, capsys) -> None:
    client = _Client()
    monkeypatch.setattr(cli, "_langsmith_client", lambda: client)
    monkeypatch.setattr(
        cli.ProviderConfig,
        "from_env",
        staticmethod(_RealConfig),
    )
    monkeypatch.setattr(cli, "_git_commit", lambda: "abc123")
    async def run_experiment(client, settings):
        return SimpleNamespace(
            experiment_id="experiment-id",
            experiment_name=settings.run_name,
            experiment_url="https://smith.invalid/experiment",
            example_ids=(str(EXAMPLE_ID),),
            run_ids=("run-id",),
        )

    monkeypatch.setattr(
        cli,
        "run_langsmith_runtime_regression_experiment",
        run_experiment,
    )
    monkeypatch.setattr(
        cli,
        "wait_for_langsmith_runtime_regression_completeness",
        lambda *args, **kwargs: SimpleNamespace(
            run_ids=("run-id",),
            feedback={str(EXAMPLE_ID): {"score-key": True}},
        ),
    )

    assert cli.main(
        [
            "--run",
            "--run-name",
            "r1",
            "--no-env-file",
            "--allow-real-provider",
            "--allow-runtime-side-effects",
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["backend"] == "langsmith"
    assert output["experiment_id"] == "experiment-id"
    assert output["run_ids"] == ["run-id"]
    assert output["feedback"][str(EXAMPLE_ID)] == {"score-key": True}
