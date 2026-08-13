"""LangSmith Dataset evaluator rules for Release Review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from evals.langsmith_runtime_regression.evaluators import (
    structured_langsmith_evaluator,
    validate_langsmith_model_configuration,
)

from .langsmith_backend import RELEASE_REVIEW_DATASET


RELEASE_GROUNDING_FEEDBACK_KEY = "assistant_agent.quality.grounding"
RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY = "assistant_agent.quality.response_quality"
RELEASE_REVIEW_LLM_FEEDBACK_KEYS = (
    RELEASE_GROUNDING_FEEDBACK_KEY,
    RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY,
)
RELEASE_REVIEW_RULE_NAMES = (
    "assistant-agent-release-review-grounding",
    "assistant-agent-release-review-response-quality",
)


@dataclass(frozen=True)
class ReleaseEvaluatorRuleAction:
    rule_name: str
    feedback_key: str
    action: Literal["create", "update"]
    rule_id: str | None


@dataclass(frozen=True)
class ReleaseEvaluatorConfigurationResult:
    status: str
    dataset_id: str
    rules: tuple[ReleaseEvaluatorRuleAction, ...]


def release_review_evaluator_rule_payloads(
    *,
    dataset_id: str,
    model_config_id: str,
    model_settings: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Build one Release Review Dataset rule per remote LLM judge."""

    evaluators = (
        structured_langsmith_evaluator(
            feedback_key=RELEASE_GROUNDING_FEEDBACK_KEY,
            schema_title="Assistant Agent Release Review grounding feedback",
            description=(
                "True only when material claims in the response are supported "
                "by the actual Runtime evidence and remain consistent with the "
                "Git-owned task contract."
            ),
            system_prompt=(
                "You are a strict Release Review grounding judge. Compare every "
                "material claim in the assistant response with the actual Runtime "
                "evidence and the Git-owned tool/state contract. Return false for "
                "unsupported, contradicted, fabricated, or unobserved claims."
            ),
            human_prompt=(
                "User request:\n{{request}}\n\nAssistant response:\n{{response}}"
                "\n\nRuntime evidence:\n{{evidence}}\n\nExpected tool contract:\n"
                "{{tool_contract}}\n\nExpected state assertions:\n{{state_assertions}}"
            ),
            variable_mapping={
                "request": "input.request",
                "response": "output.response",
                "evidence": "output.evidence",
                "tool_contract": "reference.tool_contract",
                "state_assertions": "reference.state_assertions",
            },
            model_config_id=model_config_id,
            model_settings=model_settings,
        ),
        structured_langsmith_evaluator(
            feedback_key=RELEASE_RESPONSE_QUALITY_FEEDBACK_KEY,
            schema_title="Assistant Agent Release Review response quality feedback",
            description=(
                "True only when the response directly, correctly, clearly, and "
                "sufficiently answers the Release Review request."
            ),
            system_prompt=(
                "You are evaluating Release Review response quality. Return true "
                "only when the response is relevant, correct, clear, internally "
                "consistent, and sufficiently complete. Do not reward verbosity."
            ),
            human_prompt=(
                "User request:\n{{request}}\n\nAssistant response:\n{{response}}"
            ),
            variable_mapping={
                "request": "input.request",
                "response": "output.response",
            },
            model_config_id=model_config_id,
            model_settings=model_settings,
        ),
    )
    return tuple(
        {
            "display_name": rule_name,
            "dataset_id": dataset_id,
            "sampling_rate": 1.0,
            "is_enabled": True,
            "evaluators": [evaluator],
        }
        for rule_name, evaluator in zip(
            RELEASE_REVIEW_RULE_NAMES,
            evaluators,
            strict=True,
        )
    )


def configure_release_review_evaluators(
    client: Any,
    *,
    model_config_id: str,
    apply: bool,
) -> ReleaseEvaluatorConfigurationResult:
    """Plan or reconcile the two fixed Release Review LLM evaluator rules."""

    dataset = client.read_dataset(dataset_name=RELEASE_REVIEW_DATASET)
    dataset_id = _required_id(dataset, "Release Review Dataset")
    model_settings = validate_langsmith_model_configuration(client, model_config_id)
    payloads = release_review_evaluator_rule_payloads(
        dataset_id=dataset_id,
        model_config_id=model_config_id,
        model_settings=model_settings,
    )
    rules = _response_json(client.request_with_retries("GET", "/runs/rules"))
    if not isinstance(rules, list):
        raise RuntimeError("LangSmith evaluator rules response must be a list")

    plans: list[ReleaseEvaluatorRuleAction] = []
    for payload, feedback_key in zip(
        payloads,
        RELEASE_REVIEW_LLM_FEEDBACK_KEYS,
        strict=True,
    ):
        matching = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("display_name") == payload["display_name"]
        ]
        if len(matching) > 1:
            raise RuntimeError(
                "duplicate LangSmith Release Review evaluator rule "
                f"{payload['display_name']!r}"
            )
        rule_id: str | None = None
        if matching:
            owned = matching[0]
            if str(owned.get("dataset_id") or "") != dataset_id:
                raise RuntimeError(
                    "owned Release Review evaluator rule targets the wrong Dataset: "
                    f"{payload['display_name']!r}"
                )
            if not owned.get("id"):
                raise RuntimeError(
                    "owned Release Review evaluator rule has no id: "
                    f"{payload['display_name']!r}"
                )
            rule_id = str(owned["id"])
        plans.append(
            ReleaseEvaluatorRuleAction(
                rule_name=payload["display_name"],
                feedback_key=feedback_key,
                action="update" if rule_id else "create",
                rule_id=rule_id,
            )
        )
    if not apply:
        return ReleaseEvaluatorConfigurationResult(
            status=_status(plans, planned=True),
            dataset_id=dataset_id,
            rules=tuple(plans),
        )

    written: list[ReleaseEvaluatorRuleAction] = []
    for payload, plan in zip(payloads, plans, strict=True):
        method = "PATCH" if plan.action == "update" else "POST"
        path = f"/runs/rules/{plan.rule_id}" if plan.rule_id else "/runs/rules"
        result = _response_json(
            client.request_with_retries(
                method,
                path,
                request_kwargs={"json": payload},
            )
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("LangSmith Release Review rule write returned no id")
        if str(result.get("dataset_id") or "") != dataset_id:
            raise RuntimeError(
                "LangSmith Release Review rule write targeted the wrong Dataset"
            )
        written.append(
            ReleaseEvaluatorRuleAction(
                rule_name=plan.rule_name,
                feedback_key=plan.feedback_key,
                action=plan.action,
                rule_id=str(result["id"]),
            )
        )
    return ReleaseEvaluatorConfigurationResult(
        status=_status(plans, planned=False),
        dataset_id=dataset_id,
        rules=tuple(written),
    )


def _status(plans: list[ReleaseEvaluatorRuleAction], *, planned: bool) -> str:
    actions = {plan.action for plan in plans}
    if actions == {"create"}:
        return "planned_create" if planned else "created"
    if actions == {"update"}:
        return "planned_update" if planned else "updated"
    return "planned_reconcile" if planned else "reconciled"


def _required_id(value: Any, label: str) -> str:
    identifier = str(
        value.get("id") if isinstance(value, dict) else getattr(value, "id", "")
    )
    if not identifier:
        raise RuntimeError(f"{label} has no id")
    return identifier


def _response_json(response: Any) -> Any:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    return response.json()
