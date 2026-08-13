from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from evals.langsmith_runtime_regression.evaluators import (
    REQUIRED_LANGSMITH_FEEDBACK_KEYS,
    configure_runtime_regression_evaluators,
    runtime_regression_evaluator_rule_payloads,
)


MODEL_CONFIG_ID = "00000000-0000-0000-0000-000000000123"
DATASET_ID = "00000000-0000-0000-0000-000000000456"
MODEL_SETTINGS = {
    "model": "gpt-4.1-mini",
    "model_provider": "openai",
    "temperature": 0,
}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class Client:
    def __init__(self, rules, *, model_configurations=None):
        self.rules = rules
        self.writes = []
        self.model_configurations = (
            model_configurations
            if model_configurations is not None
            else [
                {
                    "id": MODEL_CONFIG_ID,
                    "available_in_evaluators": True,
                    "settings": deepcopy(MODEL_SETTINGS),
                }
            ]
        )

    def read_dataset(self, *, dataset_name):
        assert dataset_name == "assistant-agent-runtime-regressions"
        return SimpleNamespace(id=DATASET_ID)

    def request_with_retries(self, method, path, request_kwargs=None):
        if (method, path) == ("GET", "/playground-settings?scope=workspace"):
            return Response(self.model_configurations)
        if (method, path) == ("GET", "/runs/rules"):
            return Response(self.rules)
        payload = request_kwargs["json"]
        self.writes.append((method, path, payload))
        rule_id = (
            path.rsplit("/", 1)[-1]
            if method == "PATCH"
            else (f"created-{len(self.writes)}")
        )
        return Response({"id": rule_id, "dataset_id": DATASET_ID})


def test_runtime_evaluator_payloads_use_one_llm_judge_per_rule() -> None:
    payloads = runtime_regression_evaluator_rule_payloads(
        dataset_id=DATASET_ID,
        model_config_id=MODEL_CONFIG_ID,
        model_settings=MODEL_SETTINGS,
    )

    assert len(payloads) == 3
    assert len({payload["display_name"] for payload in payloads}) == 3
    assert all(payload["dataset_id"] == DATASET_ID for payload in payloads)
    assert all(len(payload["evaluators"]) == 1 for payload in payloads)
    assert all(
        payload["evaluators"][0]["structured"]["model"] == MODEL_SETTINGS
        for payload in payloads
    )
    assert all(
        payload["evaluators"][0]["structured"]["playground_settings_id"]
        == MODEL_CONFIG_ID
        for payload in payloads
    )
    schemas = [payload["evaluators"][0]["structured"]["schema"] for payload in payloads]
    assert len({schema["title"] for schema in schemas}) == 3
    assert len({schema["description"] for schema in schemas}) == 3
    assert all(0 < len(schema["title"]) <= 120 for schema in schemas)
    assert all(0 < len(schema["description"]) <= 500 for schema in schemas)
    assert tuple(
        payload["evaluators"][0]["structured"]["variable_mapping"]
        for payload in payloads
    ) == (
        {"request": "input.content", "response": "output.content"},
        {
            "request": "input.content",
            "response": "output.content",
            "evidence": "output.evaluation_evidence",
        },
        {
            "request": "input.content",
            "baseline": "reference.content",
            "response": "output.content",
        },
    )
    assert (
        tuple(
            next(iter(payload["evaluators"][0]["structured"]["schema"]["properties"]))
            for payload in payloads
        )
        == REQUIRED_LANGSMITH_FEEDBACK_KEYS
    )


def test_runtime_evaluator_rules_are_independently_idempotent() -> None:
    payloads = runtime_regression_evaluator_rule_payloads(
        dataset_id=DATASET_ID,
        model_config_id=MODEL_CONFIG_ID,
        model_settings=MODEL_SETTINGS,
    )
    client = Client(
        [
            {
                "id": "existing-response-quality",
                "dataset_id": DATASET_ID,
                "display_name": payloads[0]["display_name"],
            },
            {
                "id": "operator-owned-other-rule",
                "dataset_id": DATASET_ID,
                "display_name": "operator-owned",
            },
        ]
    )

    result = configure_runtime_regression_evaluators(
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
            REQUIRED_LANGSMITH_FEEDBACK_KEYS[0],
            "update",
            "existing-response-quality",
        ),
        (
            payloads[1]["display_name"],
            REQUIRED_LANGSMITH_FEEDBACK_KEYS[1],
            "create",
            "created-2",
        ),
        (
            payloads[2]["display_name"],
            REQUIRED_LANGSMITH_FEEDBACK_KEYS[2],
            "create",
            "created-3",
        ),
    )
    assert [(method, path) for method, path, _payload in client.writes] == [
        ("PATCH", "/runs/rules/existing-response-quality"),
        ("POST", "/runs/rules"),
        ("POST", "/runs/rules"),
    ]
    assert all(len(payload["evaluators"]) == 1 for _, _, payload in client.writes)
    assert all(
        payload["evaluators"][0]["structured"]["model"] == MODEL_SETTINGS
        for _, _, payload in client.writes
    )
    assert all(
        payload["evaluators"][0]["structured"]["playground_settings_id"]
        == MODEL_CONFIG_ID
        for _, _, payload in client.writes
    )


@pytest.mark.parametrize(
    "conflicting_rule",
    [
        {
            "id": "wrong-dataset-rule",
            "dataset_id": "00000000-0000-0000-0000-000000000999",
        },
        {"dataset_id": DATASET_ID},
    ],
)
def test_owned_rule_name_conflict_fails_closed(conflicting_rule) -> None:
    payload = runtime_regression_evaluator_rule_payloads(
        dataset_id=DATASET_ID,
        model_config_id=MODEL_CONFIG_ID,
        model_settings=MODEL_SETTINGS,
    )[0]
    client = Client([{**conflicting_rule, "display_name": payload["display_name"]}])

    with pytest.raises(RuntimeError, match="owned evaluator rule"):
        configure_runtime_regression_evaluators(
            client,
            model_config_id=MODEL_CONFIG_ID,
            apply=True,
        )

    assert client.writes == []


@pytest.mark.parametrize(
    "model_configurations",
    [
        [
            {"id": MODEL_CONFIG_ID, "available_in_evaluators": True},
            {"id": MODEL_CONFIG_ID, "available_in_evaluators": True},
        ],
        [{"id": MODEL_CONFIG_ID}],
        [{"id": MODEL_CONFIG_ID, "available_in_evaluators": False}],
        [{"id": MODEL_CONFIG_ID, "available_in_evaluators": 1}],
    ],
)
def test_model_configuration_must_be_unique_and_strictly_available(
    model_configurations,
) -> None:
    client = Client([], model_configurations=model_configurations)

    with pytest.raises(RuntimeError, match="model configuration"):
        configure_runtime_regression_evaluators(
            client,
            model_config_id=MODEL_CONFIG_ID,
            apply=True,
        )

    assert client.writes == []


@pytest.mark.parametrize(
    "settings",
    [
        None,
        [],
        {},
        {1: "non-string-key"},
        {"model": object()},
        {"model": float("nan")},
    ],
)
def test_model_configuration_settings_must_be_a_strict_json_object(settings) -> None:
    configuration = {
        "id": MODEL_CONFIG_ID,
        "available_in_evaluators": True,
    }
    if settings is not None:
        configuration["settings"] = settings
    client = Client([], model_configurations=[configuration])

    with pytest.raises(RuntimeError, match="model configuration settings"):
        configure_runtime_regression_evaluators(
            client,
            model_config_id=MODEL_CONFIG_ID,
            apply=True,
        )

    assert client.writes == []


@pytest.mark.parametrize(
    "settings",
    [
        {"model": "gpt-4.1-mini", "api_key": "secret"},
        {
            "model": "gpt-4.1-mini",
            "provider": {"clientSecret": "secret"},
        },
        {
            "model": "gpt-4.1-mini",
            "transport": [{"access-token": "secret"}],
        },
    ],
)
def test_model_configuration_settings_reject_credential_like_keys(settings) -> None:
    client = Client(
        [],
        model_configurations=[
            {
                "id": MODEL_CONFIG_ID,
                "available_in_evaluators": True,
                "settings": settings,
            }
        ],
    )

    with pytest.raises(RuntimeError, match="credential-like"):
        configure_runtime_regression_evaluators(
            client,
            model_config_id=MODEL_CONFIG_ID,
            apply=True,
        )

    assert client.writes == []


@pytest.mark.parametrize("secret_ids", [["QWEN_API_KEY"], ("QWEN_API_KEY",)])
def test_model_configuration_settings_allow_strict_langchain_secret_reference(
    secret_ids,
) -> None:
    settings = {
        "model": "qwen-plus",
        "model_provider": "openai",
        "kwargs": {
            "openai_api_key": {
                "id": secret_ids,
                "lc": 1,
                "type": "secret",
            }
        },
    }
    client = Client(
        [],
        model_configurations=[
            {
                "id": MODEL_CONFIG_ID,
                "available_in_evaluators": True,
                "settings": settings,
            }
        ],
    )

    configure_runtime_regression_evaluators(
        client,
        model_config_id=MODEL_CONFIG_ID,
        apply=True,
    )

    expected = deepcopy(settings)
    expected["kwargs"]["openai_api_key"]["id"] = ["QWEN_API_KEY"]
    assert all(
        payload["evaluators"][0]["structured"]["model"] == expected
        for _, _, payload in client.writes
    )


@pytest.mark.parametrize(
    "secret_reference",
    [
        {"id": [], "lc": 1, "type": "secret"},
        {"id": ["lowercase_key"], "lc": 1, "type": "secret"},
        {"id": ["QWEN_API_KEY"], "lc": True, "type": "secret"},
        {"id": ["QWEN_API_KEY"], "lc": 1, "type": "password"},
        {"id": ["QWEN_API_KEY"], "lc": 1, "type": "secret", "value": "raw"},
        {
            "id": ["A", "B", "C", "D", "E"],
            "lc": 1,
            "type": "secret",
        },
    ],
)
def test_model_configuration_settings_reject_invalid_secret_reference(
    secret_reference,
) -> None:
    client = Client(
        [],
        model_configurations=[
            {
                "id": MODEL_CONFIG_ID,
                "available_in_evaluators": True,
                "settings": {
                    "model": "qwen-plus",
                    "kwargs": {"openai_api_key": secret_reference},
                },
            }
        ],
    )

    with pytest.raises(RuntimeError, match="credential-like"):
        configure_runtime_regression_evaluators(
            client,
            model_config_id=MODEL_CONFIG_ID,
            apply=True,
        )

    assert client.writes == []


def test_partial_dry_run_reports_each_rule_action_and_identity() -> None:
    payloads = runtime_regression_evaluator_rule_payloads(
        dataset_id=DATASET_ID,
        model_config_id=MODEL_CONFIG_ID,
        model_settings=MODEL_SETTINGS,
    )
    client = Client(
        [
            {
                "id": "existing-grounding",
                "dataset_id": DATASET_ID,
                "display_name": payloads[1]["display_name"],
            }
        ]
    )

    result = configure_runtime_regression_evaluators(
        client,
        model_config_id=MODEL_CONFIG_ID,
        apply=False,
    )

    assert result.status == "planned_reconcile"
    assert tuple(
        (item.rule_name, item.feedback_key, item.action, item.rule_id)
        for item in result.rules
    ) == (
        (
            payloads[0]["display_name"],
            REQUIRED_LANGSMITH_FEEDBACK_KEYS[0],
            "create",
            None,
        ),
        (
            payloads[1]["display_name"],
            REQUIRED_LANGSMITH_FEEDBACK_KEYS[1],
            "update",
            "existing-grounding",
        ),
        (
            payloads[2]["display_name"],
            REQUIRED_LANGSMITH_FEEDBACK_KEYS[2],
            "create",
            None,
        ),
    )
    assert client.writes == []
