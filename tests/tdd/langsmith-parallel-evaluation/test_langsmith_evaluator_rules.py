from __future__ import annotations

from types import SimpleNamespace

from evals.langsmith_runtime_regression.evaluators import (
    REQUIRED_LANGSMITH_FEEDBACK_KEYS,
    configure_runtime_regression_evaluators,
    runtime_regression_evaluator_rule_payloads,
)


MODEL_CONFIG_ID = "00000000-0000-0000-0000-000000000123"
DATASET_ID = "00000000-0000-0000-0000-000000000456"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class Client:
    def __init__(self, rules):
        self.rules = rules
        self.writes = []

    def read_dataset(self, *, dataset_name):
        assert dataset_name == "assistant-agent-runtime-regressions"
        return SimpleNamespace(id=DATASET_ID)

    def request_with_retries(self, method, path, request_kwargs=None):
        if (method, path) == ("GET", "/playground-settings?scope=workspace"):
            return Response(
                [
                    {
                        "id": MODEL_CONFIG_ID,
                        "available_in_evaluators": True,
                    }
                ]
            )
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
    )

    assert len(payloads) == 3
    assert len({payload["display_name"] for payload in payloads}) == 3
    assert all(payload["dataset_id"] == DATASET_ID for payload in payloads)
    assert all(len(payload["evaluators"]) == 1 for payload in payloads)
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
    assert result.rule_ids == (
        "existing-response-quality",
        "created-2",
        "created-3",
    )
    assert [(method, path) for method, path, _payload in client.writes] == [
        ("PATCH", "/runs/rules/existing-response-quality"),
        ("POST", "/runs/rules"),
        ("POST", "/runs/rules"),
    ]
    assert all(len(payload["evaluators"]) == 1 for _, _, payload in client.writes)
