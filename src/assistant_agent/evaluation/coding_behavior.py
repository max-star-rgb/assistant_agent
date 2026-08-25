"""Strict, side-effect-free contracts for the coding behavior system eval."""

from __future__ import annotations

import re
from hashlib import sha256
import json
import unicodedata
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


SCHEMA_VERSION = 1
TRUSTED_FIXTURE_IDS = (
    "multi-file-interface-v1",
    "regression-test-required-v1",
    "scope-discipline-v1",
    "single-file-logic-bug-v1",
)
TRUSTED_GRADER_IDS = (
    "bounded_execution",
    "changed_path_scope",
    "forbidden_paths_unchanged",
    "held_out_tests",
    "integration_binding",
    "native_lifecycle",
    "terminal_status",
)
ALLOWED_INTERRUPT_KINDS = (
    "patch_approval",
    "coding_review_decision",
    "merge_approval",
)
CODING_EVAL_ERROR_CODES = (
    "coding_eval_configuration_error",
    "coding_eval_case_invalid",
    "coding_eval_server_unavailable",
    "coding_eval_repository_not_bound",
    "coding_eval_unknown_run_outcome",
    "coding_eval_unknown_interrupt",
    "coding_eval_interrupt_budget_exceeded",
    "coding_eval_deadline_exceeded",
    "coding_eval_terminal_mismatch",
    "coding_eval_grader_failed",
    "coding_eval_cleanup_pending",
)

_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-v[1-9][0-9]*$")
_PATH_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_PATHS = 64
_MAX_GRADERS = len(TRUSTED_GRADER_IDS)
_MAX_TAGS = 16
_MAX_CASES = 32


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_text(value: str, *, field_name: str, max_chars: int) -> str:
    canonical = unicodedata.normalize("NFC", value).strip()
    if not canonical or canonical != value:
        raise ValueError(f"{field_name} must be non-empty canonical NFC text")
    if len(value) > max_chars or len(value.encode("utf-8")) > max_chars * 4:
        raise ValueError(f"{field_name} exceeds its bounded size")
    return value


def _canonical_identifier(value: str, *, field_name: str) -> str:
    _canonical_text(value, field_name=field_name, max_chars=96)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a versioned lowercase identifier")
    return value


def _canonical_path(value: str) -> str:
    _canonical_text(value, field_name="path", max_chars=240)
    if "\\" in value or "\x00" in value:
        raise ValueError("path must use canonical POSIX separators")
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", ".."}:
        raise ValueError("path must be repository-relative")
    if path.as_posix() != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must be lexically normalized")
    if any(_PATH_PART.fullmatch(part) is None for part in path.parts):
        raise ValueError("path contains a noncanonical segment")
    if path.parts[0].endswith(":") or ".git" in path.parts:
        raise ValueError("path targets a reserved or host-specific location")
    return value


def _require_sorted_unique(values: tuple[str, ...], *, field_name: str) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field_name} contains a duplicate value")
    if tuple(sorted(values)) != values:
        raise ValueError(f"{field_name} must be sorted canonically")
    return values


def _manifest_digest(value: BaseModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _validate_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("manifest_digest must be a lowercase SHA-256 digest")
    return value


class CodingBehaviorExecutionProfile(_Contract):
    """Frozen v1 capability profile; provider-native execution is out of scope."""

    schema_version: Literal[1]
    provider_native_code_execution: Literal["disabled"]


class CodingBehaviorCase(_Contract):
    """One trusted, bounded coding behavior case from the tracked manifest."""

    schema_version: Literal[1]
    case_id: StrictStr
    title: StrictStr
    request: StrictStr
    fixture_id: Literal[
        "multi-file-interface-v1",
        "regression-test-required-v1",
        "scope-discipline-v1",
        "single-file-logic-bug-v1",
    ]
    expected_changed_paths: tuple[StrictStr, ...]
    allowed_changed_paths: tuple[StrictStr, ...]
    forbidden_changed_paths: tuple[StrictStr, ...]
    grader_ids: tuple[
        Literal[
            "bounded_execution",
            "changed_path_scope",
            "forbidden_paths_unchanged",
            "held_out_tests",
            "integration_binding",
            "native_lifecycle",
            "terminal_status",
        ],
        ...,
    ]
    max_runtime_seconds: StrictInt = Field(ge=1, le=3600)
    required_interrupts: tuple[
        Literal["patch_approval", "coding_review_decision", "merge_approval"], ...
    ]
    tags: tuple[StrictStr, ...]

    @field_validator("case_id")
    @classmethod
    def _validate_case_id(cls, value: str) -> str:
        return _canonical_identifier(value, field_name="case_id")

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _canonical_text(value, field_name="title", max_chars=160)

    @field_validator("request")
    @classmethod
    def _validate_request(cls, value: str) -> str:
        return _canonical_text(value, field_name="request", max_chars=2000)

    @field_validator(
        "expected_changed_paths", "allowed_changed_paths", "forbidden_changed_paths"
    )
    @classmethod
    def _validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > _MAX_PATHS:
            raise ValueError("path tuple exceeds its budget")
        canonical = tuple(_canonical_path(value) for value in values)
        return _require_sorted_unique(canonical, field_name="path tuple")

    @field_validator("grader_ids")
    @classmethod
    def _validate_graders(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) > _MAX_GRADERS:
            raise ValueError("grader_ids exceeds its non-empty budget")
        return _require_sorted_unique(values, field_name="grader_ids")

    @field_validator("required_interrupts")
    @classmethod
    def _validate_interrupts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("required_interrupts contains a duplicate value")
        positions = tuple(ALLOWED_INTERRUPT_KINDS.index(value) for value in values)
        if positions != tuple(sorted(positions)):
            raise ValueError("required_interrupts must follow canonical lifecycle order")
        return values

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) > _MAX_TAGS:
            raise ValueError("tags exceeds its non-empty budget")
        for value in values:
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None:
                raise ValueError("tags must use canonical lowercase tokens")
        return _require_sorted_unique(values, field_name="tags")

    @model_validator(mode="after")
    def _validate_path_sets(self) -> Self:
        expected = set(self.expected_changed_paths)
        allowed = set(self.allowed_changed_paths)
        forbidden = set(self.forbidden_changed_paths)
        if not expected:
            raise ValueError("expected_changed_paths must be non-empty")
        if not expected.issubset(allowed):
            raise ValueError("expected_changed_paths must be a subset of allowed_changed_paths")
        if not allowed.isdisjoint(forbidden):
            raise ValueError("allowed_changed_paths and forbidden_changed_paths must be disjoint")
        return self


class CodingBehaviorCaseBinding(_Contract):
    """Redacted binding from one trusted case to its required grader inventory."""

    schema_version: Literal[1]
    case_id: StrictStr
    fixture_id: StrictStr
    manifest_digest: StrictStr
    grader_ids: tuple[StrictStr, ...]

    @field_validator("manifest_digest")
    @classmethod
    def _validate_manifest_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("grader_ids")
    @classmethod
    def _validate_grader_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or any(value not in TRUSTED_GRADER_IDS for value in values):
            raise ValueError("grader_ids must be a non-empty trusted inventory")
        return _require_sorted_unique(values, field_name="grader_ids")

    @classmethod
    def from_case(cls, case: CodingBehaviorCase) -> Self:
        return cls(
            schema_version=SCHEMA_VERSION,
            case_id=case.case_id,
            fixture_id=case.fixture_id,
            manifest_digest=_manifest_digest(case),
            grader_ids=case.grader_ids,
        )


class CodingBehaviorSuite(_Contract):
    schema_version: Literal[1]
    suite_id: StrictStr
    execution_profile: CodingBehaviorExecutionProfile
    cases: tuple[CodingBehaviorCase, ...]

    @field_validator("suite_id")
    @classmethod
    def _validate_suite_id(cls, value: str) -> str:
        return _canonical_identifier(value, field_name="suite_id")

    @field_validator("cases")
    @classmethod
    def _validate_cases(cls, values: tuple[CodingBehaviorCase, ...]) -> tuple[CodingBehaviorCase, ...]:
        if not values or len(values) > _MAX_CASES:
            raise ValueError("cases exceeds its non-empty budget")
        case_ids = tuple(case.case_id for case in values)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("cases contains a duplicate case_id")
        if tuple(sorted(case_ids)) != case_ids:
            raise ValueError("cases must be sorted canonically by case_id")
        fixture_ids = tuple(case.fixture_id for case in values)
        if len(set(fixture_ids)) != len(fixture_ids):
            raise ValueError("cases contains a duplicate fixture_id")
        return values


class CodingBehaviorSuiteBinding(_Contract):
    """Redacted binding from one trusted suite to its complete case inventory."""

    schema_version: Literal[1]
    suite_id: StrictStr
    manifest_digest: StrictStr
    execution_profile: CodingBehaviorExecutionProfile
    cases: tuple[CodingBehaviorCaseBinding, ...]

    @field_validator("manifest_digest")
    @classmethod
    def _validate_manifest_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("cases")
    @classmethod
    def _validate_cases(
        cls, values: tuple[CodingBehaviorCaseBinding, ...]
    ) -> tuple[CodingBehaviorCaseBinding, ...]:
        if not values:
            raise ValueError("suite binding requires a non-empty case inventory")
        case_ids = tuple(value.case_id for value in values)
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("suite binding contains a duplicate case_id")
        if tuple(sorted(case_ids)) != case_ids:
            raise ValueError("suite binding cases must be sorted canonically")
        return values

    @classmethod
    def from_suite(cls, suite: CodingBehaviorSuite) -> Self:
        return cls(
            schema_version=SCHEMA_VERSION,
            suite_id=suite.suite_id,
            manifest_digest=_manifest_digest(suite),
            execution_profile=suite.execution_profile,
            cases=tuple(CodingBehaviorCaseBinding.from_case(case) for case in suite.cases),
        )


class CodingBehaviorError(_Contract):
    schema_version: Literal[1]
    code: Literal[
        "coding_eval_configuration_error",
        "coding_eval_case_invalid",
        "coding_eval_server_unavailable",
        "coding_eval_repository_not_bound",
        "coding_eval_unknown_run_outcome",
        "coding_eval_unknown_interrupt",
        "coding_eval_interrupt_budget_exceeded",
        "coding_eval_deadline_exceeded",
        "coding_eval_terminal_mismatch",
        "coding_eval_grader_failed",
        "coding_eval_cleanup_pending",
    ]
    message: StrictStr

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        return _canonical_text(value, field_name="error.message", max_chars=512)


class CodingBehaviorCheckResult(_Contract):
    schema_version: Literal[1]
    check_id: Literal[
        "bounded_execution",
        "changed_path_scope",
        "forbidden_paths_unchanged",
        "held_out_tests",
        "integration_binding",
        "native_lifecycle",
        "terminal_status",
    ]
    status: Literal["passed", "failed"]
    error: CodingBehaviorError | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        if (self.status == "passed") != (self.error is None):
            raise ValueError("passed checks cannot contain errors and failed checks require one")
        return self


class CodingBehaviorCaseResult(_Contract):
    schema_version: Literal[1]
    case_id: StrictStr
    fixture_id: Literal[
        "multi-file-interface-v1",
        "regression-test-required-v1",
        "scope-discipline-v1",
        "single-file-logic-bug-v1",
    ]
    case_binding: CodingBehaviorCaseBinding
    status: Literal["passed", "failed"]
    checks: tuple[CodingBehaviorCheckResult, ...]
    error: CodingBehaviorError | None = None
    terminal_status: StrictStr | None = None
    changed_paths: tuple[StrictStr, ...]
    elapsed_ms: StrictInt = Field(ge=0, le=3_600_000)
    cleanup_pending: StrictBool

    @field_validator("case_id")
    @classmethod
    def _validate_case_id(cls, value: str) -> str:
        return _canonical_identifier(value, field_name="case_id")

    @field_validator("changed_paths")
    @classmethod
    def _validate_changed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > _MAX_PATHS:
            raise ValueError("changed_paths exceeds its budget")
        return _require_sorted_unique(
            tuple(_canonical_path(value) for value in values), field_name="changed_paths"
        )

    @field_validator("checks")
    @classmethod
    def _validate_checks(
        cls, values: tuple[CodingBehaviorCheckResult, ...]
    ) -> tuple[CodingBehaviorCheckResult, ...]:
        ids = tuple(value.check_id for value in values)
        if len(set(ids)) != len(ids):
            raise ValueError("checks contains a duplicate check_id")
        if tuple(sorted(ids)) != ids:
            raise ValueError("checks must be sorted canonically")
        return values

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        if (
            self.case_id != self.case_binding.case_id
            or self.fixture_id != self.case_binding.fixture_id
        ):
            raise ValueError("case result identity must match its manifest binding")
        check_ids = tuple(check.check_id for check in self.checks)
        if not set(check_ids).issubset(self.case_binding.grader_ids):
            raise ValueError("checks contain an ID outside the bound grader inventory")
        if self.status == "passed":
            if self.error is not None or self.cleanup_pending:
                raise ValueError("passed case cannot contain an error or cleanup debt")
            if any(check.status != "passed" for check in self.checks):
                raise ValueError("passed case requires all checks to pass")
            if check_ids != self.case_binding.grader_ids:
                raise ValueError("passed case requires the complete grader inventory")
        elif self.error is None:
            raise ValueError("failed case requires an error")
        return self


class CodingBehaviorSuiteResult(_Contract):
    schema_version: Literal[1]
    suite_id: StrictStr
    execution_profile: CodingBehaviorExecutionProfile
    suite_binding: CodingBehaviorSuiteBinding
    status: Literal["passed", "failed"]
    cases: tuple[CodingBehaviorCaseResult, ...]
    elapsed_ms: StrictInt = Field(ge=0, le=115_200_000)
    error: CodingBehaviorError | None = None

    @field_validator("suite_id")
    @classmethod
    def _validate_suite_id(cls, value: str) -> str:
        return _canonical_identifier(value, field_name="suite_id")

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        case_ids = tuple(case.case_id for case in self.cases)
        bound_case_ids = tuple(case.case_id for case in self.suite_binding.cases)
        if (
            self.suite_id != self.suite_binding.suite_id
            or self.execution_profile != self.suite_binding.execution_profile
        ):
            raise ValueError("suite result identity must match its manifest binding")
        if case_ids != bound_case_ids:
            raise ValueError("suite result requires the exact manifest case inventory")
        for result, binding in zip(self.cases, self.suite_binding.cases, strict=True):
            if result.case_binding != binding:
                raise ValueError("case result binding does not match the suite manifest")
        if self.status == "passed":
            if self.error is not None or any(case.status != "passed" for case in self.cases):
                raise ValueError("passed suite requires every case to pass without an error")
        elif self.error is None and all(case.status == "passed" for case in self.cases):
            raise ValueError("failed suite requires a failed case or suite error")
        return self


def validate_coding_behavior_case_result(
    case: CodingBehaviorCase,
    result: CodingBehaviorCaseResult,
) -> CodingBehaviorCaseResult:
    """Validate a result against the actual validated case authority.

    Runners must use this boundary rather than accepting a result's self-declared
    binding as proof of the required grader inventory.
    """

    expected_binding = CodingBehaviorCaseBinding.from_case(case)
    if result.case_binding != expected_binding:
        raise ValueError("case result binding does not match the actual validated case")
    return result


def validate_coding_behavior_suite_result(
    suite: CodingBehaviorSuite,
    result: CodingBehaviorSuiteResult,
) -> CodingBehaviorSuiteResult:
    """Validate a result against the actual validated suite authority."""

    expected_binding = CodingBehaviorSuiteBinding.from_suite(suite)
    if result.suite_binding != expected_binding:
        raise ValueError("suite result binding does not match the actual validated suite")
    for case, case_result in zip(suite.cases, result.cases, strict=True):
        validate_coding_behavior_case_result(case, case_result)
    return result


class CodingBehaviorDryRunCase(_Contract):
    schema_version: Literal[1]
    case_id: StrictStr
    fixture_id: StrictStr
    grader_ids: tuple[StrictStr, ...]
    required_interrupts: tuple[StrictStr, ...]
    max_runtime_seconds: StrictInt


class CodingBehaviorDryRunReport(_Contract):
    schema_version: Literal[1]
    suite_id: StrictStr
    mode: Literal["dry_run"]
    execution_profile: CodingBehaviorExecutionProfile
    case_count: StrictInt
    cases: tuple[CodingBehaviorDryRunCase, ...]
    provider_call_planned: Literal[False]
    server_connection_planned: Literal[False]
    repository_mutation_planned: Literal[False]


def build_coding_behavior_dry_run(
    suite: CodingBehaviorSuite,
) -> CodingBehaviorDryRunReport:
    """Project a validated suite without reading environment or causing I/O."""

    cases = tuple(
        CodingBehaviorDryRunCase(
            schema_version=SCHEMA_VERSION,
            case_id=case.case_id,
            fixture_id=case.fixture_id,
            grader_ids=case.grader_ids,
            required_interrupts=case.required_interrupts,
            max_runtime_seconds=case.max_runtime_seconds,
        )
        for case in suite.cases
    )
    return CodingBehaviorDryRunReport(
        schema_version=SCHEMA_VERSION,
        suite_id=suite.suite_id,
        mode="dry_run",
        execution_profile=suite.execution_profile,
        case_count=len(cases),
        cases=cases,
        provider_call_planned=False,
        server_connection_planned=False,
        repository_mutation_planned=False,
    )


__all__ = [
    "ALLOWED_INTERRUPT_KINDS",
    "CODING_EVAL_ERROR_CODES",
    "CodingBehaviorCase",
    "CodingBehaviorCaseBinding",
    "CodingBehaviorCaseResult",
    "CodingBehaviorCheckResult",
    "CodingBehaviorDryRunCase",
    "CodingBehaviorDryRunReport",
    "CodingBehaviorError",
    "CodingBehaviorExecutionProfile",
    "CodingBehaviorSuite",
    "CodingBehaviorSuiteBinding",
    "CodingBehaviorSuiteResult",
    "SCHEMA_VERSION",
    "TRUSTED_FIXTURE_IDS",
    "TRUSTED_GRADER_IDS",
    "build_coding_behavior_dry_run",
    "validate_coding_behavior_case_result",
    "validate_coding_behavior_suite_result",
]
