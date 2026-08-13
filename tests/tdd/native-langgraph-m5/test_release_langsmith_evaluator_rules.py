from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import evals.release_review.cli as cli
from evals.release_review.evaluators import (
    RELEASE_GROUNDING_FEEDBACK_KEY,
    RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY,
    configure_release_review_evaluators,
    release_review_evaluator_rule_payloads,
)


MODEL_CONFIG_ID = "00000000-0000-0000-0000-000000000123"
DATASET_ID = "00000000-0000-0000-0000-000000000456"
MODEL_SETTINGS = {
    "model": "qwen-plus",
    "model_provider": "openai",
    "kwargs": {
        "openai_api_key": {
            "id": ["DASHSCOPE_API_KEY"],
            "lc": 1,
            "type": "secret",
        }
    },
}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class Client:
    def __init__(self, rules=(), *, settings=MODEL_SETTINGS):
        self.rules = list(rules)
        self.settings = settings
        self.calls = []
        self.flushed = False
        self.closed = False

    def read_dataset(self, *, dataset_name):
        assert dataset_name == "assistant-agent-release-review"
        return SimpleNamespace(id=DATASET_ID)

    def request_with_retries(self, method, path, request_kwargs=None):
        self.calls.append((method, path, request_kwargs))
        if (method, path) == ("GET", "/playground-settings?scope=workspace"):
            return Response(
                [
                    {
                        "id": MODEL_CONFIG_ID,
                        "available_in_evaluators": True,
                        "settings": deepcopy(self.settings),
                    }
                ]
            )
        if (method, path) == ("GET", "/runs/rules"):
            return Response(self.rules)
        payload = request_kwargs["json"]
        rule_id = (
            path.rsplit("/", 1)[-1]
            if method == "PATCH"
            else f"created-{len([call for call in self.calls if call[0] == 'POST'])}"
        )
        return Response({"id": rule_id, "dataset_id": DATASET_ID, **payload})

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True


def test_release_rules_are_independent_and_use_validated_model_settings() -> None:
    payloads = release_review_evaluator_rule_payloads(
        dataset_id=DATASET_ID,
        model_config_id=MODEL_CONFIG_ID,
        model_settings=MODEL_SETTINGS,
    )

    assert len(payloads) == 2
    assert all(len(payload["evaluators"]) == 1 for payload in payloads)
    structured = [payload["evaluators"][0]["structured"] for payload in payloads]
    assert all(item["model"] == MODEL_SETTINGS for item in structured)
    assert all(item["playground_settings_id"] == MODEL_CONFIG_ID for item in structured)
    assert all(item["schema"]["title"] for item in structured)
    assert all(item["schema"]["description"] for item in structured)
    assert tuple(next(iter(item["schema"]["properties"])) for item in structured) == (
        RELEASE_GROUNDING_FEEDBACK_KEY,
        RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY,
    )
    assert structured[0]["variable_mapping"] == {
        "request": "input.request",
        "response": "output.response",
        "evidence": "output.evidence",
        "tool_contract": "reference.tool_contract",
        "state_assertions": "reference.state_assertions",
    }
    assert structured[1]["variable_mapping"] == {
        "request": "input.request",
        "response": "output.response",
    }


def test_release_rules_reconcile_independently_and_preserve_mapping() -> None:
    payloads = release_review_evaluator_rule_payloads(
        dataset_id=DATASET_ID,
        model_config_id=MODEL_CONFIG_ID,
        model_settings=MODEL_SETTINGS,
    )
    client = Client(
        [
            {
                "id": "existing-grounding",
                "dataset_id": DATASET_ID,
                "display_name": payloads[0]["display_name"],
            }
        ]
    )

    result = configure_release_review_evaluators(
        client,
        model_config_id=MODEL_CONFIG_ID,
        apply=True,
    )

    assert result.status == "reconciled"
    assert tuple(
        (item.rule_name, item.feedback_key, item.action, item.rule_id)
        for item in result.rules
    ) == (
        (
            payloads[0]["display_name"],
            RELEASE_GROUNDING_FEEDBACK_KEY,
            "update",
            "existing-grounding",
        ),
        (
            payloads[1]["display_name"],
            RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY,
            "create",
            "created-1",
        ),
    )
    writes = [call for call in client.calls if call[0] in {"PATCH", "POST"}]
    assert [(method, path) for method, path, _kwargs in writes] == [
        ("PATCH", "/runs/rules/existing-grounding"),
        ("POST", "/runs/rules"),
    ]
    assert all(len(kwargs["json"]["evaluators"]) == 1 for _, _, kwargs in writes)


@pytest.mark.parametrize(
    "settings",
    [
        None,
        [],
        {"model": "qwen-plus", "api_key": "raw-secret"},
    ],
)
def test_release_rules_reuse_strict_model_settings_before_rule_reads(settings) -> None:
    client = Client(settings=settings)

    with pytest.raises(RuntimeError, match="model configuration settings"):
        configure_release_review_evaluators(
            client,
            model_config_id=MODEL_CONFIG_ID,
            apply=True,
        )

    assert [(method, path) for method, path, _kwargs in client.calls] == [
        ("GET", "/playground-settings?scope=workspace")
    ]


def test_release_cli_reports_rule_plan_without_running_experiment(
    monkeypatch,
    capsys,
) -> None:
    client = Client()
    experiment_called = False

    def fail_experiment(*_args, **_kwargs):
        nonlocal experiment_called
        experiment_called = True
        raise AssertionError("configuration must not run an Experiment")

    monkeypatch.setattr(cli, "_langsmith_client", lambda: client)
    monkeypatch.setattr(cli, "run_release_experiment", fail_experiment, raising=False)

    assert (
        cli.main(
            [
                "--configure-evaluators",
                "--model-config-id",
                MODEL_CONFIG_ID,
                "--no-env-file",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "configure_evaluators"
    assert output["status"] == "planned_create"
    assert [item["feedback_key"] for item in output["rules"]] == [
        RELEASE_GROUNDING_FEEDBACK_KEY,
        RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY,
    ]
    assert output["apply"] is False
    assert experiment_called is False
    assert client.flushed is True
    assert client.closed is True
