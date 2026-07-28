"""Versioned Langfuse eval taxonomy, profiles, suites, and item selection."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


EVAL_MANIFEST_PATH = Path(
    "evals/cases/langfuse/eval_manifest_v1.json"
)


class EvalProfile(BaseModel):
    """How one compatible Langfuse Dataset is executed."""

    dataset_name: str = Field(min_length=1)
    seed_source: Path
    experiment_name: str = Field(min_length=1)
    default_suite: str = Field(min_length=1)
    chat_mode: Literal["scripted", "real"]
    tool_mode: Literal[
        "simulated",
        "readonly_mixed",
        "configured_system",
    ]


class EvalSuite(BaseModel):
    """A named selection of capabilities within one execution profile."""

    profile: str = Field(min_length=1)
    description: str = Field(min_length=1)
    all_cases: bool = False
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selector(self) -> "EvalSuite":
        if self.all_cases == bool(self.capabilities):
            raise ValueError(
                "Eval suite must select either all cases or capabilities."
            )
        return self


class EvalScoreNames(BaseModel):
    deterministic: list[str] = Field(min_length=1)
    semantic: list[str] = Field(min_length=1)


class EvalManifest(BaseModel):
    """Single local index for eval profiles, suites, and stable capabilities."""

    schema_version: Literal[
        "assistant_agent_eval_manifest_v1"
    ] = "assistant_agent_eval_manifest_v1"
    profiles: dict[str, EvalProfile]
    suites: dict[str, EvalSuite]
    capabilities: dict[str, str]
    capability_aliases: dict[str, str] = Field(default_factory=dict)
    score_names: EvalScoreNames

    @model_validator(mode="after")
    def validate_references(self) -> "EvalManifest":
        capability_names = set(self.capabilities)
        for profile_name, profile in self.profiles.items():
            suite = self.suites.get(profile.default_suite)
            if suite is None or suite.profile != profile_name:
                raise ValueError(
                    f"Profile {profile_name!r} has an invalid default suite."
                )
        for suite_name, suite in self.suites.items():
            if suite.profile not in self.profiles:
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
    case_ids: Collection[str] = (),
    capabilities: Collection[str] = (),
) -> list[str]:
    """Select item ids with AND across selector types and OR within each type."""

    suite = manifest.suites.get(suite_name)
    if suite is None:
        raise ValueError(f"Unknown eval suite: {suite_name!r}.")
    requested_case_ids = set(case_ids)
    requested_capabilities = {
        manifest.normalize_capability(value) for value in capabilities
    }
    records = [
        (
            str(_item_value(item, "id")),
            manifest.normalize_capability(
                str((_item_value(item, "metadata") or {}).get("capability", ""))
            ),
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
    available_capabilities = {capability for _, capability in records}
    missing_capabilities = requested_capabilities - available_capabilities
    if missing_capabilities:
        raise ValueError(
            "Dataset capabilities are unavailable: "
            + ", ".join(sorted(missing_capabilities))
            + "."
        )

    suite_capabilities = set(suite.capabilities)
    selected = [
        case_id
        for case_id, capability in records
        if (suite.all_cases or capability in suite_capabilities)
        and (not requested_case_ids or case_id in requested_case_ids)
        and (
            not requested_capabilities
            or capability in requested_capabilities
        )
    ]
    if not selected:
        raise ValueError("Eval selectors did not match any Dataset items.")
    return selected


def _item_value(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)
