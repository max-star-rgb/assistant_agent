"""Shared lifecycle template for controlled Agent eval Environments."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from assistant_agent.config import ProviderConfig
from assistant_agent.runtime.chat_adapter import ChatAdapter
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.tools.registry import ToolRegistry
from evals.agent.contracts import (
    AssertionResult,
    EnvironmentValidation,
    TaskExecution,
    TaskSpec,
    ToolOutcomeExpectation,
)
from evals.agent.grading import environment_validation, rule_assertion
from evals.agent.task_support import (
    StateReader,
    execute_agent_eval_runtime,
    execute_isolated_runtime,
    outcome_expectations,
    subset_registry,
)
from evals.agent.registry_overlay import (
    EvalRegistryAssembly,
    EvalToolReplacement,
    apply_tool_replacements,
)


@dataclass(frozen=True)
class EnvironmentToolVisibility:
    """Auditable structured override for one controlled Environment."""

    profile: str
    allowed_tools: tuple[str, ...]


class ControlledTaskEnvironment:
    """Own the common Environment protocol and expose narrow Task hooks."""

    dependency_label = "controlled:task"
    writes = False
    state_reset = "per_task_run"
    tool_catalog_label = "default_complete_agent_eval_registry"

    def __init__(
        self,
        *,
        config: ProviderConfig | None = None,
        chat_adapter: ChatAdapter | None = None,
    ) -> None:
        self.config = config
        self.chat_adapter = chat_adapter
        self._registry: ToolRegistry | None = None
        self._runtime_assembly: EvalRegistryAssembly | None = None
        self.setup()

    def setup(self) -> None:
        """Prepare Task-local controlled dependencies and isolated state."""

    def build_registry(self) -> ToolRegistry:
        raise NotImplementedError

    def tool_replacements(
        self,
        production_registry: ToolRegistry,
    ) -> tuple[EvalToolReplacement, ...]:
        del production_registry
        return ()

    def required_successes(self) -> tuple[str, ...]:
        return ()

    def required_failures(self) -> Mapping[str, str]:
        return {}

    def visibility_override(self) -> EnvironmentToolVisibility | None:
        return None

    def task_validation_checks(
        self,
        registry: ToolRegistry,
    ) -> Mapping[str, AssertionResult]:
        del registry
        return {}

    def initial_state(self, request: UserRequest) -> dict[str, Any]:
        del request
        return {}

    def before_run(
        self,
        runtime: AgentGraphRuntime,
        request: UserRequest,
    ) -> None:
        del runtime, request

    def final_state_reader(self, request: UserRequest) -> StateReader | None:
        del request
        return None

    def runtime_overrides(self, request: UserRequest) -> Mapping[str, Any]:
        del request
        return {}

    @property
    def registry(self) -> ToolRegistry:
        if self._registry is None:
            registry = self.build_registry()
            if not isinstance(registry, ToolRegistry):
                raise TypeError("build_registry() must return ToolRegistry.")
            self._registry = registry
        return self._registry

    @property
    def runtime_assembly(self) -> EvalRegistryAssembly | None:
        return self._runtime_assembly

    def _uses_legacy_registry(self) -> bool:
        return type(self).build_registry is not ControlledTaskEnvironment.build_registry

    def visible_tool_names(self) -> list[str]:
        if not self._uses_legacy_registry() and self._runtime_assembly is None:
            return sorted(
                {*self.required_successes(), *self.required_failures()}
            )
        registry = (
            self.registry
            if self._uses_legacy_registry()
            else self._runtime_assembly.registry
        )
        override = self.visibility_override()
        if override is None:
            return registry.list()
        registered = set(registry.list())
        return sorted(name for name in override.allowed_tools if name in registered)

    def describe(self) -> dict[str, Any]:
        override = self.visibility_override()
        registered_tool_count = (
            len(self.registry.list())
            if self._uses_legacy_registry()
            else (
                len(self._runtime_assembly.registry.list())
                if self._runtime_assembly is not None
                else None
            )
        )
        return {
            "runtime": "AgentGraphRuntime",
            "chat_provider": "configured_real",
            "dependencies": self.dependency_label,
            "tool_catalog": self.tool_catalog_label,
            "registered_tool_count": registered_tool_count,
            "tool_visibility_profile": (
                override.profile if override is not None else None
            ),
            "writes": self.writes,
            "state_reset": self.state_reset,
        }

    def validate(self) -> EnvironmentValidation:
        if not self._uses_legacy_registry():
            successes = set(self.required_successes())
            failures = set(self.required_failures())
            checks = {
                "required_tool_outcomes_do_not_conflict": rule_assertion(
                    not successes.intersection(failures),
                    (
                        f"successes={sorted(successes)}, "
                        f"failures={sorted(failures)}"
                    ),
                    label="必需工具结果约束无冲突",
                ),
                "replacement_overlay_is_runtime_bound": rule_assertion(
                    True,
                    "production_registry_is_resolved_during_runtime_assembly",
                    label="替换层延迟到 Runtime 装配",
                ),
            }
            return environment_validation(checks)
        registry = self.registry
        return self._validate_registry(registry)

    def _validate_registry(self, registry: ToolRegistry) -> EnvironmentValidation:
        registered = set(registry.list())
        successes = set(self.required_successes())
        failures = set(self.required_failures())
        required = successes | failures
        override = self.visibility_override()
        visible = set(self.visible_tool_names())
        visibility_profile_valid = override is None or (
            bool(override.profile.strip())
            and bool(override.allowed_tools)
            and len(override.allowed_tools) == len(set(override.allowed_tools))
            and set(override.allowed_tools).issubset(registered)
        )
        required_tools_known = required.issubset(registered)
        required_tools_visible = required.issubset(visible)
        common_checks: dict[str, AssertionResult] = {
            "registry_sealed": rule_assertion(
                registry.sealed and bool(registered),
                f"sealed={registry.sealed}, registered_tool_count={len(registered)}",
                label="完整工具注册表已装配",
            ),
            "tool_visibility_valid": rule_assertion(
                visibility_profile_valid,
                (
                    f"profile={override.profile!r}, allowed_tools={override.allowed_tools}"
                    if override is not None
                    else "profile=None, default_complete_registry=True"
                ),
                label="结构化工具可见性配置有效",
            ),
            "required_tools_known": rule_assertion(
                required_tools_known,
                f"required={sorted(required)}, registered={sorted(registered)}",
                label="必需工具均已注册",
            ),
            "required_tools_visible": rule_assertion(
                required_tools_visible,
                f"required={sorted(required)}, visible={sorted(visible)}",
                label="必需工具在运行目录中可见",
            ),
        }
        if visibility_profile_valid and required_tools_known:
            expectations = self.tool_outcome_expectations()
            expectation_names = [item.tool_name for item in expectations]
            outcome_contract_valid = (
                len(expectation_names) == len(set(expectation_names))
                and set(expectation_names) == visible
            )
        else:
            expectation_names = []
            outcome_contract_valid = False
        common_checks["outcome_contract_matches_visible_tools"] = rule_assertion(
            outcome_contract_valid,
            (f"expectations={sorted(expectation_names)}, visible={sorted(visible)}"),
            label="工具结果预期覆盖可见目录",
        )
        task_checks = dict(self.task_validation_checks(registry))
        duplicate_names = set(common_checks).intersection(task_checks)
        if duplicate_names:
            raise ValueError(
                "Task validation checks conflict with shared checks: "
                f"{sorted(duplicate_names)}"
            )
        task_checks_are_rules = all(
            isinstance(assertion, AssertionResult)
            and assertion.evaluation_method == "rule"
            for assertion in task_checks.values()
        )
        common_checks["task_validation_checks_are_rules"] = rule_assertion(
            task_checks_are_rules,
            f"task_check_names={sorted(task_checks)}",
            label="Task 专属校验均为 Rule",
        )
        return environment_validation({**common_checks, **task_checks})

    def validate_runtime_registry(
        self,
        assembly: EvalRegistryAssembly,
    ) -> EnvironmentValidation:
        return self._validate_registry(assembly.registry)

    def _transform_runtime_registry(
        self,
        production_registry: ToolRegistry,
    ) -> ToolRegistry:
        assembly = apply_tool_replacements(
            production_registry,
            self.tool_replacements(production_registry),
        )
        self._runtime_assembly = assembly
        self.validate_runtime_registry(assembly).require_valid()
        return assembly.registry

    def tool_outcome_expectations(
        self,
        available_tools: list[str] | None = None,
    ) -> list[ToolOutcomeExpectation]:
        selected_names = (
            (
                self.visible_tool_names()
                if self._uses_legacy_registry() or self._runtime_assembly is not None
                else [*self.required_successes(), *self.required_failures()]
            )
            if available_tools is None
            else list(available_tools)
        )
        selected_names = list(
            dict.fromkeys(
                [
                    *selected_names,
                    *self.required_successes(),
                    *self.required_failures(),
                ]
            )
        )
        if self._uses_legacy_registry():
            source_registry = self.registry
        elif self._runtime_assembly is not None:
            source_registry = self._runtime_assembly.registry
        else:
            return [
                *[
                    ToolOutcomeExpectation.must_succeed(name)
                    for name in self.required_successes()
                ],
                *[
                    ToolOutcomeExpectation.must_fail_with(
                        name,
                        error_code=error_code,
                    )
                    for name, error_code in self.required_failures().items()
                ],
            ]
        registry = subset_registry(source_registry, selected_names)
        return outcome_expectations(
            registry,
            required_successes=self.required_successes(),
            required_failures=self.required_failures(),
        )

    def execute(
        self,
        *,
        task: TaskSpec,
        request: UserRequest | dict[str, Any],
        trace_id: str,
        parent_span_id: str,
    ) -> TaskExecution:
        self.validate().require_valid()
        resolved_request = self._request_for_runtime(
            UserRequest.model_validate(request)
        )
        initial_state = self.initial_state(resolved_request)

        def run_before(runtime: AgentGraphRuntime) -> None:
            self.before_run(runtime, resolved_request)

        if self._uses_legacy_registry():
            return execute_isolated_runtime(
                task=task,
                request=resolved_request,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                config=self.config,
                registry=self.registry,
                chat_adapter=self.chat_adapter,
                initial_state=initial_state,
                before_run=run_before,
                final_state_reader=self.final_state_reader(resolved_request),
                runtime_overrides=self.runtime_overrides(resolved_request),
            )
        return execute_agent_eval_runtime(
            task=task,
            request=resolved_request,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            config=self.config,
            registry_transform=self._transform_runtime_registry,
            provenance_reader=lambda: (
                self._runtime_assembly.provenance
                if self._runtime_assembly is not None
                else {}
            ),
            chat_adapter=self.chat_adapter,
            initial_state=initial_state,
            before_run=run_before,
            final_state_reader=self.final_state_reader(resolved_request),
            runtime_overrides=self.runtime_overrides(resolved_request),
        )

    def _request_for_runtime(self, request: UserRequest) -> UserRequest:
        override = self.visibility_override()
        if override is None:
            return request
        metadata = dict(request.metadata)
        metadata["tool_visibility"] = {
            "profile": override.profile,
            "allowed_tools": list(override.allowed_tools),
        }
        return request.model_copy(update={"metadata": metadata})
