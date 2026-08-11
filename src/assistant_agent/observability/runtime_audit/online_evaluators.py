"""Version-controlled setup for Langfuse native live observation evaluators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.evaluation.constants import RUNTIME_REGRESSION_DATASET

from langfuse.api.unstable.commons.types.evaluation_rule_filter import (
    EvaluationRuleFilter_StringObject,
    EvaluationRuleFilter_StringOptions,
)
from langfuse.api.unstable.commons.types.evaluation_rule_mapping import (
    EvaluationRuleMapping,
)
from langfuse.api.unstable.commons.types.evaluation_rule_mapping_source import (
    EvaluationRuleMappingSource,
)
from langfuse.api.unstable.commons.types.evaluation_rule_options_filter_operator import (
    EvaluationRuleOptionsFilterOperator,
)
from langfuse.api.unstable.commons.types.evaluation_rule_string_filter_operator import (
    EvaluationRuleStringFilterOperator,
)
from langfuse.api.unstable.commons.types.evaluation_rule_target import (
    EvaluationRuleTarget,
)
from langfuse.api.unstable.commons.types.evaluator_model_config import EvaluatorModelConfig
from langfuse.api.unstable.commons.types.evaluator_output_definition import (
    EvaluatorOutputDefinition_Boolean,
)
from langfuse.api.unstable.commons.types.evaluator_output_field_definition import (
    EvaluatorOutputFieldDefinition,
)
from langfuse.api.unstable.commons.types.evaluator_scope import EvaluatorScope
from langfuse.api.unstable.evaluation_rules.types.create_llm_as_judge_evaluation_rule_request import (
    CreateLlmAsJudgeEvaluationRuleRequest,
)
from langfuse.api.unstable.evaluation_rules.types.llm_as_judge_evaluation_rule_evaluator_reference import (
    LlmAsJudgeEvaluationRuleEvaluatorReference,
)
from langfuse.api.unstable.evaluation_rules.types.llm_as_judge_evaluator_type import (
    LlmAsJudgeEvaluatorType,
)
from langfuse.api.unstable.evaluators.types.create_evaluator_request import (
    CreateEvaluatorRequest_LlmAsJudge,
)


class OnlineEvaluatorConfigurationResult(BaseModel):
    applied: bool
    evaluator_names: list[str]
    rule_names: list[str]
    created_evaluators: int = 0
    existing_evaluators: int = 0
    updated_evaluators: int = 0
    created_rules: int = 0
    existing_rules: int = 0
    updated_rules: int = 0
    skipped_rule_names: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _EvaluatorSpec:
    name: str
    prompt: str


@dataclass(frozen=True)
class _RuleSpec:
    name: str
    evaluator_name: str
    target: EvaluationRuleTarget
    observation_name: str | None
    observation_type: str | None
    dataset_id: str | None = None
    legacy_rule_names: tuple[str, ...] = ()
    metadata_filters: tuple[tuple[str, str], ...] = ()
    mappings: tuple["_RuleMappingSpec", ...] = ()
    trace_names: tuple[str, ...] = ("assistant.turn",)
    environments: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RuleMappingSpec:
    variable: str
    source: EvaluationRuleMappingSource
    json_path: str | None = None


_DEFAULT_RULE_MAPPINGS = (
    _RuleMappingSpec("input", EvaluationRuleMappingSource.INPUT),
    _RuleMappingSpec("output", EvaluationRuleMappingSource.OUTPUT),
)


def configure_native_online_evaluators(
    client: Any,
    *,
    apply: bool,
    model_provider: str,
    model: str,
) -> OnlineEvaluatorConfigurationResult:
    """Reconcile live and Runtime Regression evaluator families and rules."""

    evaluator_specs = _specs()
    rule_specs = _rule_specs(dataset_id=None)
    evaluator_resource = client.api.unstable.evaluators
    rule_resource = client.api.unstable.evaluation_rules
    existing_evaluators = {
        _field(item, "name"): item
        for item in _response_data(evaluator_resource.list())
        if _scope_value(_field(item, "scope")) == "project"
    }
    existing_rules = {
        _field(item, "name"): item for item in _response_data(rule_resource.list())
    }
    result = OnlineEvaluatorConfigurationResult(
        applied=apply,
        evaluator_names=[item.name for item in evaluator_specs],
        rule_names=[item.name for item in rule_specs],
    )
    if not apply:
        return result
    dataset_id = _runtime_regression_dataset_id(client)
    rule_specs = _rule_specs(dataset_id=dataset_id)

    created_evaluators = 0
    existing_evaluator_count = 0
    updated_evaluators = 0
    created_rules = 0
    existing_rule_count = 0
    updated_rules = 0
    skipped_rule_names: list[str] = []
    for spec in evaluator_specs:
        evaluator_request = _evaluator_request(
            spec,
            model_provider=model_provider,
            model=model,
        )
        if spec.name not in existing_evaluators:
            evaluator_resource.create(request=evaluator_request)
            created_evaluators += 1
        else:
            existing_evaluator_count += 1
            if _evaluator_has_drifted(existing_evaluators[spec.name], evaluator_request):
                evaluator_resource.create(request=evaluator_request)
                updated_evaluators += 1
    for spec in rule_specs:
        if spec.target == EvaluationRuleTarget.EXPERIMENT and spec.dataset_id is None:
            skipped_rule_names.append(spec.name)
            continue
        rule_request = _rule_request(spec)
        existing_rule = existing_rules.get(spec.name)
        legacy_rule = next(
            (
                existing_rules[name]
                for name in spec.legacy_rule_names
                if name in existing_rules
            ),
            None,
        )
        if existing_rule is None:
            if legacy_rule is None:
                rule_resource.create(request=rule_request)
                created_rules += 1
            else:
                rule_id = _field(legacy_rule, "id")
                if not isinstance(rule_id, str) or not rule_id:
                    raise RuntimeError(
                        f"Legacy Langfuse evaluation rule for {spec.name!r} has no id."
                    )
                changes: dict[str, Any] = {
                    "name": spec.name,
                    **_rule_update_fields(rule_request),
                }
                rule_resource.update(rule_id, **changes)
                existing_rule_count += 1
                updated_rules += 1
        else:
            rule_id = _field(existing_rule, "id")
            if not isinstance(rule_id, str) or not rule_id:
                raise RuntimeError(
                    f"Langfuse evaluation rule {spec.name!r} has no id."
                )
            existing_rule_count += 1
            rule_resource.update(rule_id, **_rule_update_fields(rule_request))
            updated_rules += 1
    return result.model_copy(
        update={
            "created_evaluators": created_evaluators,
            "existing_evaluators": existing_evaluator_count,
            "updated_evaluators": updated_evaluators,
            "created_rules": created_rules,
            "existing_rules": existing_rule_count,
            "updated_rules": updated_rules,
            "skipped_rule_names": skipped_rule_names,
        }
    )


def _evaluator_request(
    spec: _EvaluatorSpec,
    *,
    model_provider: str,
    model: str,
) -> CreateEvaluatorRequest_LlmAsJudge:
    return CreateEvaluatorRequest_LlmAsJudge(
        name=spec.name,
        prompt=spec.prompt,
        output_definition=EvaluatorOutputDefinition_Boolean(
            reasoning=EvaluatorOutputFieldDefinition(
                description="用简短中文说明判断依据；只引用输入和输出中的可见证据。"
            ),
            score=EvaluatorOutputFieldDefinition(
                description="满足该质量维度时为 true，否则为 false。"
            ),
        ),
        model_config_=EvaluatorModelConfig(provider=model_provider, model=model),
    )


def _rule_request(spec: _RuleSpec) -> CreateLlmAsJudgeEvaluationRuleRequest:
    if spec.target == EvaluationRuleTarget.EXPERIMENT:
        if spec.dataset_id is None:
            raise ValueError(f"experiment rule {spec.name!r} has no Dataset id")
        filters = [
            EvaluationRuleFilter_StringOptions(
                column="datasetId",
                operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                value=[spec.dataset_id],
            )
        ]
    else:
        if spec.observation_type is None:
            raise ValueError(f"observation rule {spec.name!r} has no observation type")
        filters = [
            EvaluationRuleFilter_StringOptions(
                column="type",
                operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                value=[spec.observation_type],
            ),
        ]
        if spec.environments:
            filters.append(
                EvaluationRuleFilter_StringOptions(
                    column="environment",
                    operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                    value=list(spec.environments),
                )
            )
        if spec.trace_names:
            filters.append(
                EvaluationRuleFilter_StringOptions(
                    column="traceName",
                    operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                    value=list(spec.trace_names),
                )
            )
        if spec.observation_name is not None:
            filters.append(
                EvaluationRuleFilter_StringOptions(
                    column="name",
                    operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
                    value=[spec.observation_name],
                )
            )
        filters.extend(
            EvaluationRuleFilter_StringObject(
                column="metadata",
                key=key,
                operator=EvaluationRuleStringFilterOperator.EQUALS,
                value=value,
            )
            for key, value in spec.metadata_filters
        )
    return CreateLlmAsJudgeEvaluationRuleRequest(
        name=spec.name,
        evaluator=LlmAsJudgeEvaluationRuleEvaluatorReference(
            name=spec.evaluator_name,
            scope=EvaluatorScope.PROJECT,
            type=LlmAsJudgeEvaluatorType.LLM_AS_JUDGE,
        ),
        target=spec.target,
        enabled=True,
        sampling=1.0,
        filter=filters,
        mapping=[
            _rule_mapping_request(mapping)
            for mapping in (spec.mappings or _DEFAULT_RULE_MAPPINGS)
        ],
    )


def _rule_mapping_request(spec: _RuleMappingSpec) -> EvaluationRuleMapping:
    if spec.json_path is None:
        return EvaluationRuleMapping(variable=spec.variable, source=spec.source)
    return EvaluationRuleMapping(
        variable=spec.variable,
        source=spec.source,
        json_path=spec.json_path,
    )


def _rule_update_fields(
    request: CreateLlmAsJudgeEvaluationRuleRequest,
) -> dict[str, Any]:
    return {
        "evaluator": request.evaluator,
        "target": request.target,
        "filter": request.filter,
        "mapping": request.mapping,
    }


def _specs() -> tuple[_EvaluatorSpec, ...]:
    return (
        _EvaluatorSpec(
            name="assistant_agent.quality.response_quality",
            prompt=(
                "判断 Assistant 输出是否清晰、完整、直接回应当前用户请求。"
                "不要因为没有调用工具而扣分，除非请求确实需要外部证据。\n\n"
                "完整模型输入：\n{{input}}\n\nAssistant 输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.grounding",
            prompt=(
                "判断 Assistant 输出中的事实断言是否忠于模型输入里可见的工具结果、上下文和失败状态，"
                "不得把缺失证据补成确定事实。无需外部证据的普通对话可判 true。\n\n"
                "完整模型输入：\n{{input}}\n\nAssistant 输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.tool_result_quality",
            prompt=(
                "判断单次工具输出相对于工具输入是否语义正确、内部一致且可供后续 Agent 使用。"
                "Provider 超时、损坏数据或不可解释错误判 false；合法且明确的空结果可判 true。\n\n"
                "工具输入：\n{{input}}\n\n工具输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.memory_extraction",
            prompt=(
                "判断长期记忆变更是否忠于本轮完整对话，只保留稳定、未来有用、属于用户的事实或偏好；"
                "不得保存临时请求、助手猜测、工具瞬时结果或过期事实。没有值得保存的内容且 changes 为空时判 true。\n\n"
                "本轮对话：\n{{input}}\n\nMem0 changes：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.memory_recall",
            prompt=(
                "检查模型输入中的长期记忆上下文是否与当前用户请求相关、未压过当前请求、且没有把明显陈旧的"
                "动态事实当作当前事实。输出能正确忽略无关记忆时也可判 true。\n\n"
                "完整模型输入（含可用记忆上下文）：\n{{input}}\n\nAssistant 输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.grounding.experiment",
            prompt=(
                "判断当前 Assistant 回答中的事实断言是否忠于本次真实 Runtime 执行证据。"
                "input 是从 experiment-item-task 独立记录的 calls、tool_results、errors 和终态；"
                "不得把缺失或失败的工具证据补成确定事实。无需外部证据的普通对话可判 true。\n\n"
                "Runtime 执行证据：\n{{input}}\n\n当前 Assistant 输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.regression_improvement",
            prompt=(
                "判断当前真实 Runtime 输出是否消除了原始失败案例中的问题，且没有引入同等或更严重的新问题。"
                "baseline 是人工沉淀时的原始失败输出，不是 golden answer，也不要求当前回答模仿其措辞。"
                "优先结合 case metadata 中的故障分类；若 metadata 未说明，则比较两次回答的完整性、事实依据、"
                "工具使用和失败处理。原问题仍存在、当前结果更差或证据不足时判 false；原输出实际无明显问题且"
                "当前结果保持同等质量时可判 true。\n\n用户输入：\n{{input}}\n\n原始失败 baseline：\n{{baseline}}"
                "\n\n当前 Assistant 输出：\n{{output}}\n\n案例 metadata：\n{{case_metadata}}"
            ),
        ),
    )


def _rule_specs(*, dataset_id: str | None) -> tuple[_RuleSpec, ...]:
    final_text_filter = (("assistant_agent.runtime_action", "text"),)
    live = (
        _RuleSpec(
            name="assistant_agent.quality.response_quality",
            evaluator_name="assistant_agent.quality.response_quality",
            target=EvaluationRuleTarget.OBSERVATION,
            legacy_rule_names=(
                "assistant-agent-live-response-quality",
                "assistant_agent.quality.response_quality.live",
            ),
            observation_name="llm.chat",
            observation_type="GENERATION",
            metadata_filters=final_text_filter,
        ),
        _RuleSpec(
            name="assistant_agent.quality.grounding",
            evaluator_name="assistant_agent.quality.grounding",
            target=EvaluationRuleTarget.OBSERVATION,
            legacy_rule_names=(
                "assistant-agent-live-grounding",
                "assistant_agent.quality.grounding.live",
            ),
            observation_name="llm.chat",
            observation_type="GENERATION",
            metadata_filters=final_text_filter,
        ),
        _RuleSpec(
            name="assistant_agent.quality.tool_result_quality",
            evaluator_name="assistant_agent.quality.tool_result_quality",
            target=EvaluationRuleTarget.OBSERVATION,
            legacy_rule_names=(
                "assistant-agent-live-tool-result-quality",
                "assistant_agent.quality.tool_result_quality.live",
            ),
            observation_name=None,
            observation_type="SPAN",
            metadata_filters=(("assistant_agent.observation_kind", "tool_execution"),),
        ),
        _RuleSpec(
            name="assistant_agent.quality.memory_extraction",
            evaluator_name="assistant_agent.quality.memory_extraction",
            target=EvaluationRuleTarget.OBSERVATION,
            legacy_rule_names=(
                "assistant-agent-live-memory-extraction",
                "assistant_agent.quality.memory_extraction.live",
            ),
            observation_name="memory.turn_ingestion",
            observation_type="SPAN",
            metadata_filters=(("assistant_agent.memory_semantic_evidence", "available"),),
        ),
        _RuleSpec(
            name="assistant_agent.quality.memory_recall",
            evaluator_name="assistant_agent.quality.memory_recall",
            target=EvaluationRuleTarget.OBSERVATION,
            legacy_rule_names=(
                "assistant-agent-live-memory-recall",
                "assistant_agent.quality.memory_recall.live",
            ),
            observation_name="llm.chat",
            observation_type="GENERATION",
            metadata_filters=final_text_filter,
        ),
    )
    return live + (
        _RuleSpec(
            name="assistant_agent.quality.response_quality.experiment",
            evaluator_name="assistant_agent.quality.response_quality",
            target=EvaluationRuleTarget.EXPERIMENT,
            observation_name=None,
            observation_type=None,
            dataset_id=dataset_id,
        ),
        _RuleSpec(
            name="assistant_agent.quality.grounding.experiment",
            evaluator_name="assistant_agent.quality.grounding.experiment",
            target=EvaluationRuleTarget.OBSERVATION,
            observation_name="runtime-regression-evidence",
            observation_type="SPAN",
            trace_names=(),
            environments=("sdk-experiment",),
        ),
        _RuleSpec(
            name="assistant_agent.quality.regression_improvement.experiment",
            evaluator_name="assistant_agent.quality.regression_improvement",
            target=EvaluationRuleTarget.EXPERIMENT,
            observation_name=None,
            observation_type=None,
            dataset_id=dataset_id,
            mappings=(
                _RuleMappingSpec("input", EvaluationRuleMappingSource.INPUT),
                _RuleMappingSpec("output", EvaluationRuleMappingSource.OUTPUT),
                _RuleMappingSpec(
                    "baseline",
                    EvaluationRuleMappingSource.EXPECTED_OUTPUT,
                ),
                _RuleMappingSpec(
                    "case_metadata",
                    EvaluationRuleMappingSource.EXPERIMENT_ITEM_METADATA,
                ),
            ),
        ),
    )


def _runtime_regression_dataset_id(client: Any) -> str | None:
    try:
        dataset = client.get_dataset(RUNTIME_REGRESSION_DATASET)
    except Exception as exc:
        if getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "NotFoundError":
            return None
        raise
    dataset_id = _field(dataset, "id")
    if not isinstance(dataset_id, str) or not dataset_id:
        raise RuntimeError("runtime regression Dataset has no id")
    return dataset_id


def _evaluator_has_drifted(existing: Any, desired: CreateEvaluatorRequest_LlmAsJudge) -> bool:
    existing_values = _evaluator_configuration(existing)
    if existing_values is None:
        return False
    desired_values = desired.model_dump(mode="json", by_alias=True)
    return existing_values != {
        key: desired_values[key]
        for key in ("type", "prompt", "outputDefinition", "modelConfig")
    }


def _evaluator_configuration(value: Any) -> dict[str, Any] | None:
    aliases = {
        "type": ("type",),
        "prompt": ("prompt",),
        "outputDefinition": ("outputDefinition", "output_definition"),
        "modelConfig": ("modelConfig", "model_config_", "model_config"),
    }
    result: dict[str, Any] = {}
    for target, names in aliases.items():
        field_value = None
        found = False
        for name in names:
            if isinstance(value, dict) and name in value:
                field_value = value[name]
                found = True
                break
            if not isinstance(value, dict) and hasattr(value, name):
                field_value = getattr(value, name)
                found = True
                break
        if not found:
            return None
        result[target] = _json_value(field_value)
    return result


def _json_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", by_alias=True)
    return value


def _response_data(response: Any) -> list[Any]:
    data = getattr(response, "data", response)
    return list(data)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _scope_value(value: Any) -> str | None:
    return getattr(value, "value", value)
