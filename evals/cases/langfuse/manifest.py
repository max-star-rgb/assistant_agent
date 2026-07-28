"""Versioned Langfuse eval datasets, profiles, suites, and item selection."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


EVAL_MANIFEST_PATH = Path("evals/cases/langfuse/eval_manifest_v2.json")


class EvalDataset(BaseModel):
    dataset_name: str = Field(min_length=1)
    seed_source: Path
    kind: Literal["infrastructure", "behavior"]


class EvalProfile(BaseModel):
    experiment_name: str = Field(min_length=1)
    default_suite: str = Field(min_length=1)
    chat_mode: Literal["scripted", "real"]
    tool_mode: Literal["simulated", "readonly_mixed", "configured_system"]
    allowed_effects: list[
        Literal["none", "readonly", "external_generation", "isolated_write"]
    ] = Field(min_length=1)


class EvalSuite(BaseModel):
    dataset: str = Field(min_length=1)
    default_profile: str = Field(min_length=1)
    description: str = Field(min_length=1)
    all_cases: bool = False
    case_ids: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selector(self) -> "EvalSuite":
        selector_count = sum(
            [self.all_cases, bool(self.case_ids), bool(self.capabilities)]
        )
        if selector_count != 1:
            raise ValueError(
                "Eval suite must select exactly one of all_cases, case_ids, "
                "or capabilities."
            )
        return self


class EvalScoreNames(BaseModel):
    deterministic: list[str] = Field(min_length=1)
    semantic: list[str] = Field(min_length=1)


class EvalManifest(BaseModel):
    schema_version: Literal[
        "assistant_agent_eval_manifest_v2"
    ] = "assistant_agent_eval_manifest_v2"
    datasets: dict[str, EvalDataset]
    profiles: dict[str, EvalProfile]
    suites: dict[str, EvalSuite]
    capabilities: dict[str, str]
    capability_aliases: dict[str, str] = Field(default_factory=dict)
    case_id_aliases: dict[str, str] = Field(default_factory=dict)
    score_names: EvalScoreNames
    archived_datasets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_references(self) -> "EvalManifest":
        capability_names = set(self.capabilities)
        for profile_name, profile in self.profiles.items():
            suite = self.suites.get(profile.default_suite)
            if suite is None or suite.default_profile != profile_name:
                raise ValueError(
                    f"Profile {profile_name!r} has an invalid default suite."
                )
        for suite_name, suite in self.suites.items():
            if suite.dataset not in self.datasets:
                raise ValueError(
                    f"Suite {suite_name!r} references an unknown dataset."
                )
            if suite.default_profile not in self.profiles:
                raise ValueError(
                    f"Suite {suite_name!r} references an unknown profile."
                )
            unknown = set(suite.capabilities) - capability_names
            if unknown:
                raise ValueError(
                    f"Suite {suite_name!r} has unknown capabilities: "
                    + ", ".join(sorted(unknown))
                )
        unknown_alias_targets = (
            set(self.capability_aliases.values()) - capability_names
        )
        if unknown_alias_targets:
            raise ValueError(
                "Capability aliases reference unknown targets: "
                + ", ".join(sorted(unknown_alias_targets))
            )
        return self

    def normalize_capability(self, value: str) -> str:
        return self.capability_aliases.get(value, value)


def load_eval_manifest(
    path: Path | str = EVAL_MANIFEST_PATH,
) -> EvalManifest:
    return EvalManifest.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )


def select_eval_item_ids(
    items: Iterable[Any],
    *,
    manifest: EvalManifest,
    suite_name: str,
    profile_name: str | None = None,
    case_ids: Collection[str] = (),
    capabilities: Collection[str] = (),
) -> list[str]:
    """Select compatible items; selector kinds intersect, values union."""

    suite = manifest.suites.get(suite_name)
    if suite is None:
        raise ValueError(f"Unknown eval suite: {suite_name!r}.")
    requested_case_ids = set(case_ids)
    requested_case_ids = {
        manifest.case_id_aliases.get(value, value)
        for value in requested_case_ids
    }
    requested_capabilities = {
        manifest.normalize_capability(value) for value in capabilities
    }
    records = [
        (
            str(_item_value(item, "id")),
            _metadata(item),
        )
        for item in items
    ]
    available_case_ids = {case_id for case_id, _ in records}
    missing_case_ids = requested_case_ids - available_case_ids
    if missing_case_ids:
        raise ValueError(
            "Dataset items are unavailable: "
            + ", ".join(sorted(missing_case_ids))
            + "."
        )
    available_capabilities = {
        manifest.normalize_capability(str(metadata.get("capability", "")))
        for _, metadata in records
    }
    missing_capabilities = requested_capabilities - available_capabilities
    if missing_capabilities:
        raise ValueError(
            "Dataset capabilities are unavailable: "
            + ", ".join(sorted(missing_capabilities))
            + "."
        )

    suite_case_ids = set(suite.case_ids)
    suite_capabilities = set(suite.capabilities)
    matched: list[tuple[str, dict[str, Any]]] = []
    for case_id, metadata in records:
        capability = manifest.normalize_capability(
            str(metadata.get("capability", ""))
        )
        suite_match = (
            suite.all_cases
            or case_id in suite_case_ids
            or capability in suite_capabilities
        )
        if (
            suite_match
            and (not requested_case_ids or case_id in requested_case_ids)
            and (
                not requested_capabilities
                or capability in requested_capabilities
            )
        ):
            matched.append((case_id, metadata))
    if not matched:
        raise ValueError("Eval selectors did not match any Dataset items.")
    incompatible = [
        case_id
        for case_id, metadata in matched
        if profile_name is not None
        and isinstance(
            compatible_profiles := metadata.get("compatible_profiles"),
            list,
        )
        and profile_name not in compatible_profiles
    ]
    if incompatible:
        raise ValueError(
            f"Profile {profile_name!r} is incompatible with Dataset items: "
            + ", ".join(incompatible)
            + "."
        )
    return [case_id for case_id, _ in matched]


def _metadata(item: Any) -> dict[str, Any]:
    value = _item_value(item, "metadata")
    return value if isinstance(value, dict) else {}


def _item_value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
