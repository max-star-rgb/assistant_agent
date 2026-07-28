"""Audit and synchronize Langfuse-hosted Experiment evaluator rules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from langfuse.api.unstable.commons.types import (
    EvaluationRuleFilter_StringOptions,
    EvaluationRuleOptionsFilterOperator,
    EvaluationRuleTarget,
)
from pydantic import BaseModel, Field


EVALUATOR_MANIFEST_PATH = Path(
    "evals/langfuse/evaluators/evaluator_manifest_v1.json"
)


class HostedEvaluatorDefinition(BaseModel):
    id: str = Field(min_length=1)
    langfuse_evaluator_name: str = Field(min_length=1)
    scores: list[str] = Field(min_length=1)


class HostedEvaluatorManifest(BaseModel):
    schema_version: Literal[
        "assistant_agent_evaluator_manifest_v1"
    ] = "assistant_agent_evaluator_manifest_v1"
    dataset_rules: list[str] = Field(min_length=1)
    evaluators: list[HostedEvaluatorDefinition] = Field(min_length=1)


class EvaluatorRuleBinding(BaseModel):
    evaluator_name: str
    score_names: list[str]
    rule_ids: list[str]
    enabled: bool
    status: str
    bound_dataset_ids: list[str]
    expected_dataset_ids: list[str]
    issues: list[str] = Field(default_factory=list)


class EvaluatorRuleAudit(BaseModel):
    ready: bool
    dataset_ids: dict[str, str]
    bindings: list[EvaluatorRuleBinding]


class EvaluatorRuleSyncResult(BaseModel):
    updated_rule_ids: list[str]
    audit: EvaluatorRuleAudit


def load_evaluator_manifest(
    path: Path | str = EVALUATOR_MANIFEST_PATH,
) -> HostedEvaluatorManifest:
    return HostedEvaluatorManifest.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def audit_evaluator_rules(
    client: Any,
    *,
    manifest: HostedEvaluatorManifest | None = None,
) -> EvaluatorRuleAudit:
    resolved_manifest = manifest or load_evaluator_manifest()
    dataset_ids = {
        name: str(client.get_dataset(name).id)
        for name in resolved_manifest.dataset_rules
    }
    expected_dataset_ids = sorted(dataset_ids.values())
    rules = _list_evaluation_rules(client)
    bindings = [
        _binding_for_evaluator(
            rules,
            evaluator,
            expected_dataset_ids=expected_dataset_ids,
        )
        for evaluator in resolved_manifest.evaluators
    ]
    return EvaluatorRuleAudit(
        ready=all(not binding.issues for binding in bindings),
        dataset_ids=dataset_ids,
        bindings=bindings,
    )


def sync_evaluator_rule_datasets(
    client: Any,
    *,
    manifest: HostedEvaluatorManifest | None = None,
) -> EvaluatorRuleSyncResult:
    """Bind every managed evaluator rule to the active managed Datasets."""

    resolved_manifest = manifest or load_evaluator_manifest()
    dataset_ids = sorted(
        str(client.get_dataset(name).id)
        for name in resolved_manifest.dataset_rules
    )
    rules = _list_evaluation_rules(client)
    updated_rule_ids = []
    for evaluator in resolved_manifest.evaluators:
        matching = [
            rule
            for rule in rules
            if str(rule.evaluator.name) == evaluator.langfuse_evaluator_name
        ]
        if len(matching) != 1:
            raise RuntimeError(
                "Expected exactly one Langfuse evaluation rule for "
                f"{evaluator.langfuse_evaluator_name!r}; found {len(matching)}."
            )
        rule = matching[0]
        client.api.unstable.evaluation_rules.update(
            str(rule.id),
            target=EvaluationRuleTarget.EXPERIMENT,
            filter=[
                EvaluationRuleFilter_StringOptions(
                    column="datasetId",
                    operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                    value=dataset_ids,
                )
            ],
        )
        updated_rule_ids.append(str(rule.id))
    audit = audit_evaluator_rules(client, manifest=resolved_manifest)
    if not audit.ready:
        raise RuntimeError(
            "Langfuse evaluator rules remain incomplete after synchronization: "
            + json.dumps(
                audit.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return EvaluatorRuleSyncResult(
        updated_rule_ids=updated_rule_ids,
        audit=audit,
    )


def _list_evaluation_rules(client: Any) -> list[Any]:
    rules: list[Any] = []
    page = 1
    while True:
        response = client.api.unstable.evaluation_rules.list(
            page=page,
            limit=100,
        )
        rules.extend(response.data)
        if page >= response.meta.total_pages:
            return rules
        page += 1


def _binding_for_evaluator(
    rules: list[Any],
    evaluator: HostedEvaluatorDefinition,
    *,
    expected_dataset_ids: list[str],
) -> EvaluatorRuleBinding:
    matching = [
        rule
        for rule in rules
        if str(rule.evaluator.name) == evaluator.langfuse_evaluator_name
    ]
    rule_ids = [str(rule.id) for rule in matching]
    issues = []
    if len(matching) != 1:
        issues.append(f"expected_one_rule_found_{len(matching)}")
        return EvaluatorRuleBinding(
            evaluator_name=evaluator.langfuse_evaluator_name,
            score_names=evaluator.scores,
            rule_ids=rule_ids,
            enabled=False,
            status="missing" if not matching else "duplicate",
            bound_dataset_ids=[],
            expected_dataset_ids=expected_dataset_ids,
            issues=issues,
        )

    rule = matching[0]
    status = _enum_value(rule.status)
    if not rule.enabled:
        issues.append("rule_disabled")
    if status != "active":
        issues.append(f"rule_status_{status}")
    if _enum_value(rule.target) != "experiment":
        issues.append("target_not_experiment")
    bound_dataset_ids = sorted(_dataset_ids_from_rule(rule))
    if bound_dataset_ids != expected_dataset_ids:
        issues.append("dataset_filter_mismatch")
    return EvaluatorRuleBinding(
        evaluator_name=evaluator.langfuse_evaluator_name,
        score_names=evaluator.scores,
        rule_ids=rule_ids,
        enabled=bool(rule.enabled),
        status=status,
        bound_dataset_ids=bound_dataset_ids,
        expected_dataset_ids=expected_dataset_ids,
        issues=issues,
    )


def _dataset_ids_from_rule(rule: Any) -> set[str]:
    dataset_ids: set[str] = set()
    for rule_filter in rule.filter:
        payload = rule_filter.model_dump(mode="json", by_alias=True)
        if (
            payload.get("column") == "datasetId"
            and payload.get("operator") == "any of"
            and isinstance(payload.get("value"), list)
        ):
            dataset_ids.update(str(value) for value in payload["value"])
    return dataset_ids


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()
