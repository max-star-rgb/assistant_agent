from __future__ import annotations

import json
from types import SimpleNamespace

import evals.langsmith_runtime_regression.cli as cli


class _Client:
    def flush(self):
        return True

    def close(self):
        return True


def test_configure_evaluators_defaults_to_dry_run(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(cli, "_langsmith_client", _Client)
    monkeypatch.setattr(
        cli,
        "configure_runtime_regression_evaluators",
        lambda client, **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            status="planned_create",
            dataset_id="dataset-id",
            rule_id=None,
            feedback_keys=("score-key",),
        ),
        raising=False,
    )

    assert cli.main(
        [
            "--configure-evaluators",
            "--model-config-id",
            "model-config-id",
            "--no-env-file",
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "configure_evaluators"
    assert output["status"] == "planned_create"
    assert calls == [{"model_config_id": "model-config-id", "apply": False}]


def test_configure_evaluators_requires_explicit_apply_for_write(
    monkeypatch,
    capsys,
) -> None:
    calls = []
    monkeypatch.setattr(cli, "_langsmith_client", _Client)
    monkeypatch.setattr(
        cli,
        "configure_runtime_regression_evaluators",
        lambda client, **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            status="created",
            dataset_id="dataset-id",
            rule_id="rule-id",
            feedback_keys=("score-key",),
        ),
        raising=False,
    )

    assert cli.main(
        [
            "--configure-evaluators",
            "--model-config-id",
            "model-config-id",
            "--apply",
            "--no-env-file",
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "created"
    assert output["rule_id"] == "rule-id"
    assert calls == [{"model_config_id": "model-config-id", "apply": True}]


def test_configure_evaluators_accepts_model_config_id_from_environment(
    monkeypatch,
    capsys,
) -> None:
    calls = []
    monkeypatch.setenv(
        "LANGSMITH_EVALUATOR_MODEL_CONFIG_ID",
        "environment-model-config-id",
    )
    monkeypatch.setattr(cli, "_langsmith_client", _Client)
    monkeypatch.setattr(
        cli,
        "configure_runtime_regression_evaluators",
        lambda client, **kwargs: calls.append(kwargs)
        or SimpleNamespace(
            status="planned_create",
            dataset_id="dataset-id",
            rule_id=None,
            feedback_keys=("score-key",),
        ),
    )

    assert cli.main(["--configure-evaluators", "--no-env-file"]) == 0

    capsys.readouterr()
    assert calls == [
        {"model_config_id": "environment-model-config-id", "apply": False}
    ]
