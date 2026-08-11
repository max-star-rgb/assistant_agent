from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from assistant_agent.evaluation.langsmith_trace import LangSmithExperimentBinding
from assistant_agent.observability.trace_context import (
    RuntimeExperimentTraceLink,
    RuntimeTraceContext,
)
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


def _binding() -> LangSmithExperimentBinding:
    link = RuntimeExperimentTraceLink(
        backend="langsmith",
        trace_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        parent_run_id="11111111-2222-3333-4444-555555555555",
        experiment_id="99999999-8888-7777-6666-555555555555",
        reference_example_id=str(EXAMPLE_ID),
    )
    return LangSmithExperimentBinding(
        project_id=link.experiment_id,
        trace_context=RuntimeTraceContext(
            trace_id="a" * 32,
            parent_span_id="1" * 16,
            experiment_link=link,
        ),
    )


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


def test_item_runtime_uses_langsmith_only_experiment_store(monkeypatch) -> None:
    captured = {}

    class Runtime:
        def __init__(self, *, config, trace_store):
            captured["config"] = config
            captured["trace_store"] = trace_store

    def create_store(*, project_id):
        captured["project_id"] = project_id
        return "langsmith-store"

    def create_host(builder, *, trace_store_factory, trace_context_provider):
        captured["runtime"] = builder(trace_store_factory())
        captured["trace_context"] = trace_context_provider()
        return "host"

    monkeypatch.setattr(cli, "AgentGraphRuntime", Runtime)
    monkeypatch.setattr(cli, "create_langsmith_experiment_trace_store", create_store)
    monkeypatch.setattr(cli, "create_experiment_runtime_host", create_host)
    binding = _binding()
    config = _RealConfig()

    assert cli._create_item_runtime(config, binding) == "host"
    assert captured["project_id"] == binding.project_id
    assert captured["trace_store"] == "langsmith-store"
    assert captured["trace_context"] == binding.trace_context


def test_run_reports_experiment_and_complete_feedback(monkeypatch, capsys) -> None:
    client = _Client()
    monkeypatch.setattr(cli, "_langsmith_client", lambda: client)
    monkeypatch.setattr(
        cli.ProviderConfig,
        "from_env",
        staticmethod(_RealConfig),
    )
    monkeypatch.setattr(cli, "_git_commit", lambda: "abc123")
    monkeypatch.setattr(
        cli,
        "run_langsmith_runtime_regression_experiment",
        lambda client, settings: SimpleNamespace(
            experiment_id="experiment-id",
            experiment_name=settings.run_name,
            experiment_url="https://smith.invalid/experiment",
            example_ids=(str(EXAMPLE_ID),),
            run_ids=("run-id",),
        ),
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
