"""LangSmith-owned evaluator rules for production Runtime regressions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET
from assistant_agent.evaluation.runtime_regression_contract import assistant_output
from assistant_agent.providers.provider_errors import sanitize_error_detail
from evals.release_review.evidence import ReleaseRunEvidence


RESPONSE_QUALITY_FEEDBACK_KEY = "assistant_agent.quality.response_quality.experiment"
GROUNDING_FEEDBACK_KEY = "assistant_agent.quality.grounding.experiment"
REGRESSION_IMPROVEMENT_FEEDBACK_KEY = (
    "assistant_agent.quality.regression_improvement.experiment"
)
REQUIRED_LANGSMITH_FEEDBACK_KEYS = (
    RESPONSE_QUALITY_FEEDBACK_KEY,
    GROUNDING_FEEDBACK_KEY,
    REGRESSION_IMPROVEMENT_FEEDBACK_KEY,
)
RUNTIME_REGRESSION_RULE_NAME = "assistant-agent-runtime-regression-judges"
RUNTIME_REGRESSION_RULE_NAMES = (
    f"{RUNTIME_REGRESSION_RULE_NAME}-response-quality",
    f"{RUNTIME_REGRESSION_RULE_NAME}-grounding",
    f"{RUNTIME_REGRESSION_RULE_NAME}-regression-improvement",
)


@dataclass(frozen=True)
class LangSmithEvaluatorRuleAction:
    rule_name: str
    feedback_key: str
    action: Literal["create", "update"]
    rule_id: str | None


@dataclass(frozen=True)
class LangSmithEvaluatorConfigurationResult:
    status: str
    dataset_id: str
    rules: tuple[LangSmithEvaluatorRuleAction, ...]


def langsmith_evaluator_output(state: Any, events: list[Any]) -> dict[str, Any]:
    """Add bounded Runtime evidence without changing canonical answer fields."""

    output = assistant_output(state)
    evidence = ReleaseRunEvidence.from_state(state, events).model_dump(mode="json")
    output["evaluation_evidence"] = sanitize_error_detail(evidence)
    return output


def runtime_regression_evaluator_rule_payloads(
    *,
    dataset_id: str,
    model_config_id: str,
) -> tuple[dict[str, Any], ...]:
    """Build one legal Dataset rule per independent Boolean LLM judge."""

    if not model_config_id.strip():
        raise ValueError("LangSmith evaluator model configuration id is required")
    common = {
        "dataset_id": dataset_id,
        "sampling_rate": 1.0,
        "is_enabled": True,
    }
    evaluators = (
        _structured_evaluator(
            feedback_key=RESPONSE_QUALITY_FEEDBACK_KEY,
            description=(
                "True only when the current assistant response directly, "
                "correctly, clearly, and sufficiently answers the user request."
            ),
            system_prompt=(
                "You are evaluating an assistant response. Judge only the "
                "current response quality for the user request. Return true "
                "only if it is relevant, correct, clear, internally consistent, "
                "and sufficiently complete. Do not reward verbosity."
            ),
            human_prompt=(
                "User request:\n{{request}}\n\nCurrent assistant response:\n"
                "{{response}}"
            ),
            variable_mapping={
                "request": "inputs.content",
                "response": "outputs.content",
            },
            model_config_id=model_config_id,
        ),
        _structured_evaluator(
            feedback_key=GROUNDING_FEEDBACK_KEY,
            description=(
                "True only when material claims in the current response are "
                "supported by the captured Runtime tool and state evidence."
            ),
            system_prompt=(
                "You are a strict grounding judge. Compare every material "
                "factual claim in the assistant response with the supplied "
                "Runtime evidence. Return false for unsupported, contradicted, "
                "or fabricated claims. If the response makes no factual claim "
                "requiring external evidence, return true only when that is "
                "appropriate for the request."
            ),
            human_prompt=(
                "User request:\n{{request}}\n\nCurrent assistant response:\n"
                "{{response}}\n\nRuntime evidence:\n{{evidence}}"
            ),
            variable_mapping={
                "request": "inputs.content",
                "response": "outputs.content",
                "evidence": "outputs.evaluation_evidence",
            },
            model_config_id=model_config_id,
        ),
        _structured_evaluator(
            feedback_key=REGRESSION_IMPROVEMENT_FEEDBACK_KEY,
            description=(
                "True only when the current response materially improves on "
                "the original failed response without introducing a new failure."
            ),
            system_prompt=(
                "You are evaluating a regression fix. Compare the current "
                "assistant response with the original failed response for the "
                "same request. Return true only when the current response is a "
                "material improvement and does not introduce a new correctness, "
                "relevance, or completeness failure."
            ),
            human_prompt=(
                "User request:\n{{request}}\n\nOriginal failed response:\n"
                "{{baseline}}\n\nCurrent assistant response:\n{{response}}"
            ),
            variable_mapping={
                "request": "inputs.content",
                "baseline": "reference.content",
                "response": "outputs.content",
            },
            model_config_id=model_config_id,
        ),
    )
    return tuple(
        {
            **common,
            "display_name": rule_name,
            "evaluators": [evaluator],
        }
        for rule_name, evaluator in zip(
            RUNTIME_REGRESSION_RULE_NAMES,
            evaluators,
            strict=True,
        )
    )


def configure_runtime_regression_evaluators(
    client: Any,
    *,
    model_config_id: str,
    apply: bool,
) -> LangSmithEvaluatorConfigurationResult:
    """Plan or reconcile one fixed Dataset rule per LLM evaluator."""

    dataset = client.read_dataset(dataset_name=RUNTIME_REGRESSION_DATASET)
    dataset_id = str(dataset.id)
    payloads = runtime_regression_evaluator_rule_payloads(
        dataset_id=dataset_id,
        model_config_id=model_config_id,
    )
    _validate_model_configuration(client, model_config_id)
    rules = _response_json(client.request_with_retries("GET", "/runs/rules"))
    if not isinstance(rules, list):
        raise RuntimeError("LangSmith evaluator rules response must be a list")
    plans: list[LangSmithEvaluatorRuleAction] = []
    for payload, feedback_key in zip(
        payloads,
        REQUIRED_LANGSMITH_FEEDBACK_KEYS,
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
                "duplicate LangSmith Runtime Regression evaluator rule "
                f"{payload['display_name']!r}"
            )
        rule_id: str | None = None
        if matching:
            owned = matching[0]
            if str(owned.get("dataset_id") or "") != dataset_id:
                raise RuntimeError(
                    "owned evaluator rule targets the wrong Dataset: "
                    f"{payload['display_name']!r}"
                )
            if not owned.get("id"):
                raise RuntimeError(
                    f"owned evaluator rule has no id: {payload['display_name']!r}"
                )
            rule_id = str(owned["id"])
        plans.append(
            LangSmithEvaluatorRuleAction(
                rule_name=payload["display_name"],
                feedback_key=feedback_key,
                action="update" if rule_id else "create",
                rule_id=rule_id,
            )
        )
    if not apply:
        return LangSmithEvaluatorConfigurationResult(
            status=_aggregate_status(
                plans,
                created="planned_create",
                updated="planned_update",
            ),
            dataset_id=dataset_id,
            rules=tuple(plans),
        )

    written_rules: list[LangSmithEvaluatorRuleAction] = []
    for payload, plan in zip(payloads, plans, strict=True):
        method = "PATCH" if plan.action == "update" else "POST"
        path = f"/runs/rules/{plan.rule_id}" if plan.rule_id else "/runs/rules"
        response = client.request_with_retries(
            method,
            path,
            request_kwargs={"json": payload},
        )
        written = _response_json(response)
        if not isinstance(written, dict) or not written.get("id"):
            raise RuntimeError("LangSmith evaluator rule write returned no rule id")
        if str(written.get("dataset_id")) != dataset_id:
            raise RuntimeError(
                "LangSmith evaluator rule write targeted the wrong Dataset"
            )
        written_rules.append(
            LangSmithEvaluatorRuleAction(
                rule_name=plan.rule_name,
                feedback_key=plan.feedback_key,
                action=plan.action,
                rule_id=str(written["id"]),
            )
        )
    return LangSmithEvaluatorConfigurationResult(
        status=_aggregate_status(plans, created="created", updated="updated"),
        dataset_id=dataset_id,
        rules=tuple(written_rules),
    )


def _aggregate_status(
    plans: list[LangSmithEvaluatorRuleAction],
    *,
    created: str,
    updated: str,
) -> str:
    actions = {plan.action for plan in plans}
    if actions == {"update"}:
        return updated
    if actions == {"create"}:
        return created
    return "planned_reconcile" if created.startswith("planned_") else "reconciled"


def _structured_evaluator(
    *,
    feedback_key: str,
    description: str,
    system_prompt: str,
    human_prompt: str,
    variable_mapping: dict[str, str],
    model_config_id: str,
) -> dict[str, Any]:
    return {
        "structured": {
            "prompt": [["system", system_prompt], ["human", human_prompt]],
            "template_format": "mustache",
            "schema": {
                "type": "object",
                "properties": {
                    feedback_key: {
                        "type": "boolean",
                        "description": description,
                    }
                },
                "required": [feedback_key],
                "additionalProperties": False,
            },
            "variable_mapping": variable_mapping,
            "playground_settings_id": model_config_id,
        }
    }


def _validate_model_configuration(client: Any, model_config_id: str) -> None:
    configurations = _response_json(
        client.request_with_retries(
            "GET",
            "/playground-settings?scope=workspace",
        )
    )
    if not isinstance(configurations, list):
        raise RuntimeError("LangSmith model configurations response must be a list")
    matching = [
        item
        for item in configurations
        if isinstance(item, dict) and str(item.get("id")) == model_config_id
    ]
    if len(matching) != 1:
        raise RuntimeError(
            "LangSmith evaluator model configuration must uniquely exist in "
            "the active Workspace"
        )
    if matching[0].get("available_in_evaluators") is not True:
        raise RuntimeError(
            "LangSmith model configuration is not available to evaluators"
        )


def _response_json(response: Any) -> Any:
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    return response.json()
