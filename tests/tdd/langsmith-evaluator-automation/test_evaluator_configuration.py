from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from evals.langsmith_runtime_regression import evaluators


DATASET_ID = UUID("01234567-89ab-cdef-0123-456789abcdef")


class _Response:
    def __init__(self, value):
        self._value = value

    def json(self):
        return self._value


class _Client:
    def __init__(self, rules=(), model_configurations=None):
        self.dataset = SimpleNamespace(id=DATASET_ID)
        self.rules = list(rules)
        self.model_configurations = (
            [
                {
                    "id": "model-config-id",
                    "available_in_evaluators": True,
                }
            ]
            if model_configurations is None
            else list(model_configurations)
        )
        self.writes = []

    def read_dataset(self, *, dataset_name):
        assert dataset_name == "assistant-agent-runtime-regressions"
        return self.dataset

    def request_with_retries(self, method, pathname, **kwargs):
        if method == "GET":
            if pathname == "/runs/rules":
                return _Response(self.rules)
            assert pathname == "/playground-settings?scope=workspace"
            return _Response(self.model_configurations)
        body = kwargs["request_kwargs"]["json"]
        self.writes.append((method, pathname, body))
        rule_id = (
            pathname.rsplit("/", 1)[-1]
            if method == "PATCH"
            else "99999999-8888-7777-6666-555555555555"
        )
        return _Response(
            {
                "id": rule_id,
                "dataset_id": str(DATASET_ID),
                **body,
            }
        )


def test_rule_payload_has_three_boolean_scores_and_grounding_evidence() -> None:
    payload = evaluators.runtime_regression_evaluator_rule_payload(
        dataset_id=str(DATASET_ID),
        model_config_id="model-config-id",
    )

    structured = [item["structured"] for item in payload["evaluators"]]
    feedback_keys = {
        key
        for item in structured
        for key in item["schema"]["properties"]
    }
    assert feedback_keys == set(evaluators.REQUIRED_LANGSMITH_FEEDBACK_KEYS)
    assert all(
        item["schema"]["properties"][next(iter(item["schema"]["properties"]))][
            "type"
        ]
        == "boolean"
        for item in structured
    )
    grounding = next(
        item
        for item in structured
        if "assistant_agent.quality.grounding.experiment"
        in item["schema"]["properties"]
    )
    assert grounding["variable_mapping"]["evidence"] == (
        "outputs.evaluation_evidence"
    )
    assert all(
        item["playground_settings_id"] == "model-config-id"
        for item in structured
    )


def test_configure_dry_run_does_not_mutate_remote_rules() -> None:
    client = _Client()

    result = evaluators.configure_runtime_regression_evaluators(
        client,
        model_config_id="model-config-id",
        apply=False,
    )

    assert result.status == "planned_create"
    assert result.rule_id is None
    assert client.writes == []


def test_configure_rejects_missing_model_configuration() -> None:
    client = _Client(model_configurations=[])

    try:
        evaluators.configure_runtime_regression_evaluators(
            client,
            model_config_id="missing-model-config-id",
            apply=False,
        )
    except RuntimeError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("missing model configuration must fail closed")


def test_configure_apply_updates_the_one_matching_rule() -> None:
    rule_id = "11111111-2222-3333-4444-555555555555"
    client = _Client(
        [
            {
                "id": rule_id,
                "dataset_id": str(DATASET_ID),
                "display_name": evaluators.RUNTIME_REGRESSION_RULE_NAME,
            }
        ]
    )

    result = evaluators.configure_runtime_regression_evaluators(
        client,
        model_config_id="model-config-id",
        apply=True,
    )

    assert result.status == "updated"
    assert result.rule_id == rule_id
    assert [(method, path) for method, path, _ in client.writes] == [
        ("PATCH", f"/runs/rules/{rule_id}")
    ]


def test_configure_rejects_duplicate_matching_rules() -> None:
    matching = {
        "dataset_id": str(DATASET_ID),
        "display_name": evaluators.RUNTIME_REGRESSION_RULE_NAME,
    }
    client = _Client(
        [
            {"id": "rule-1", **matching},
            {"id": "rule-2", **matching},
        ]
    )

    try:
        evaluators.configure_runtime_regression_evaluators(
            client,
            model_config_id="model-config-id",
            apply=True,
        )
    except RuntimeError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate rules must fail closed")
