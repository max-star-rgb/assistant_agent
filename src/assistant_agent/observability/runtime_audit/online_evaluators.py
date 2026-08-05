"""Version-controlled setup for Langfuse native live observation evaluators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

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
    created_rules: int = 0
    existing_rules: int = 0
    updated_rules: int = 0


@dataclass(frozen=True)
class _EvaluatorSpec:
    name: str
    rule_name: str
    prompt: str
    observation_name: str
    observation_type: str
    metadata_filters: tuple[tuple[str, str], ...] = ()


def configure_native_online_evaluators(
    client: Any,
    *,
    apply: bool,
    model_provider: str,
    model: str,
) -> OnlineEvaluatorConfigurationResult:
    """Plan or idempotently create five 100% live observation evaluator rules."""

    specs = _specs()
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
        evaluator_names=[item.name for item in specs],
        rule_names=[item.rule_name for item in specs],
    )
    if not apply:
        return result

    created_evaluators = 0
    existing_evaluator_count = 0
    created_rules = 0
    existing_rule_count = 0
    updated_rules = 0
    for spec in specs:
        if spec.name not in existing_evaluators:
            evaluator_resource.create(
                request=_evaluator_request(
                    spec,
                    model_provider=model_provider,
                    model=model,
                )
            )
            created_evaluators += 1
        else:
            existing_evaluator_count += 1
        rule_request = _rule_request(spec)
        existing_rule = existing_rules.get(spec.rule_name)
        if existing_rule is None:
            rule_resource.create(request=rule_request)
            created_rules += 1
        else:
            rule_id = _field(existing_rule, "id")
            if not isinstance(rule_id, str) or not rule_id:
                raise RuntimeError(f"Langfuse evaluation rule {spec.rule_name!r} has no id.")
            existing_rule_count += 1
    return result.model_copy(
        update={
            "created_evaluators": created_evaluators,
            "existing_evaluators": existing_evaluator_count,
            "created_rules": created_rules,
            "existing_rules": existing_rule_count,
            "updated_rules": updated_rules,
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


def _rule_request(spec: _EvaluatorSpec) -> CreateLlmAsJudgeEvaluationRuleRequest:
    filters = [
        EvaluationRuleFilter_StringOptions(
            column="type",
            operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
            value=[spec.observation_type],
        ),
        EvaluationRuleFilter_StringOptions(
            column="name",
            operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
            value=[spec.observation_name],
        ),
        EvaluationRuleFilter_StringOptions(
            column="traceName",
            operator=EvaluationRuleOptionsFilterOperator.ANY_OF,
            value=["assistant.turn"],
        ),
    ]
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
        name=spec.rule_name,
        evaluator=LlmAsJudgeEvaluationRuleEvaluatorReference(
            name=spec.name,
            scope=EvaluatorScope.PROJECT,
            type=LlmAsJudgeEvaluatorType.LLM_AS_JUDGE,
        ),
        target=EvaluationRuleTarget.OBSERVATION,
        enabled=True,
        sampling=1.0,
        filter=filters,
        mapping=[
            EvaluationRuleMapping(
                variable="input",
                source=EvaluationRuleMappingSource.INPUT,
            ),
            EvaluationRuleMapping(
                variable="output",
                source=EvaluationRuleMappingSource.OUTPUT,
            ),
        ],
    )


def _specs() -> tuple[_EvaluatorSpec, ...]:
    final_text_filter = (("assistant_agent.runtime_action", "text"),)
    return (
        _EvaluatorSpec(
            name="assistant_agent.quality.response_quality",
            rule_name="assistant-agent-live-response-quality",
            observation_name="llm.chat",
            observation_type="GENERATION",
            metadata_filters=final_text_filter,
            prompt=(
                "判断 Assistant 输出是否清晰、完整、直接回应当前用户请求。"
                "不要因为没有调用工具而扣分，除非请求确实需要外部证据。\n\n"
                "完整模型输入：\n{{input}}\n\nAssistant 输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.grounding",
            rule_name="assistant-agent-live-grounding",
            observation_name="llm.chat",
            observation_type="GENERATION",
            metadata_filters=final_text_filter,
            prompt=(
                "判断 Assistant 输出中的事实断言是否忠于模型输入里可见的工具结果、上下文和失败状态，"
                "不得把缺失证据补成确定事实。无需外部证据的普通对话可判 true。\n\n"
                "完整模型输入：\n{{input}}\n\nAssistant 输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.tool_result_quality",
            rule_name="assistant-agent-live-tool-result-quality",
            observation_name="tool.execute",
            observation_type="SPAN",
            prompt=(
                "判断单次工具输出相对于工具输入是否语义正确、内部一致且可供后续 Agent 使用。"
                "Provider 超时、损坏数据或不可解释错误判 false；合法且明确的空结果可判 true。\n\n"
                "工具输入：\n{{input}}\n\n工具输出：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.memory_extraction",
            rule_name="assistant-agent-live-memory-extraction",
            observation_name="memory.turn_ingestion",
            observation_type="SPAN",
            metadata_filters=(("assistant_agent.memory_semantic_evidence", "available"),),
            prompt=(
                "判断长期记忆变更是否忠于本轮完整对话，只保留稳定、未来有用、属于用户的事实或偏好；"
                "不得保存临时请求、助手猜测、工具瞬时结果或过期事实。没有值得保存的内容且 changes 为空时判 true。\n\n"
                "本轮对话：\n{{input}}\n\nMem0 changes：\n{{output}}"
            ),
        ),
        _EvaluatorSpec(
            name="assistant_agent.quality.memory_recall",
            rule_name="assistant-agent-live-memory-recall",
            observation_name="llm.chat",
            observation_type="GENERATION",
            metadata_filters=final_text_filter,
            prompt=(
                "检查模型输入中的长期记忆上下文是否与当前用户请求相关、未压过当前请求、且没有把明显陈旧的"
                "动态事实当作当前事实。输出能正确忽略无关记忆时也可判 true。\n\n"
                "完整模型输入（含可用记忆上下文）：\n{{input}}\n\nAssistant 输出：\n{{output}}"
            ),
        ),
    )


def _response_data(response: Any) -> list[Any]:
    data = getattr(response, "data", response)
    return list(data)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _scope_value(value: Any) -> str | None:
    return getattr(value, "value", value)
