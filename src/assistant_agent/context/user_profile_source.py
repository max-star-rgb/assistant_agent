"""Governed local structured user-profile context source."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from assistant_agent.context.models import (
    ContextSection,
    ContextSourceIssue,
    ContextSourceResult,
)
from assistant_agent.context.sources import ContextSourceRequest


USER_PROFILE_SOURCE_ID = "user_profile"
USER_PROFILE_SOURCE_REF = "editable_context:user_profile"
USER_PROFILE_FILE_NAME = "USER_PROFILES.json"
USER_PROFILE_MAX_BYTES = 128_000
USER_PROFILE_COMPILED_MAX_CHARS = 4_000

_ATTRIBUTE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SENSITIVE_ATTRIBUTE_NAMES = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


class UserProfileAttribute(BaseModel):
    """One explicitly maintained user-profile fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | int | float | bool
    source: Literal["user_confirmed", "user_setting", "operator_verified"]
    updated_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    confirmed_at: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


class UserProfileRecord(BaseModel):
    """One user-scoped profile record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    attributes: dict[str, UserProfileAttribute] = Field(
        max_length=64,
    )

    @field_validator("attributes")
    @classmethod
    def validate_attribute_names(
        cls,
        attributes: dict[str, UserProfileAttribute],
    ) -> dict[str, UserProfileAttribute]:
        for name in attributes:
            if (
                not _ATTRIBUTE_NAME_RE.fullmatch(name)
                or name in _SENSITIVE_ATTRIBUTE_NAMES
            ):
                raise ValueError("invalid or sensitive profile attribute name")
        return attributes


class UserProfilesDocument(BaseModel):
    """Versioned on-disk collection keyed by exact runtime user identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["user_profiles_v1"] = "user_profiles_v1"
    profiles: dict[str, UserProfileRecord] = Field(max_length=1_024)

    @field_validator("profiles")
    @classmethod
    def validate_user_ids(
        cls,
        profiles: dict[str, UserProfileRecord],
    ) -> dict[str, UserProfileRecord]:
        if any(not user_id.strip() or len(user_id) > 256 for user_id in profiles):
            raise ValueError("invalid user profile identity")
        return profiles


class UserProfileContextSource:
    """Load the current user's isolated record from USER_PROFILES.json."""

    source_id = USER_PROFILE_SOURCE_ID

    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        if not request.editable_context_enabled:
            return ContextSourceResult()

        root = request.source_root.expanduser().resolve(strict=False)
        candidate = root / USER_PROFILE_FILE_NAME
        resolved_candidate = candidate.resolve(strict=False)
        if not resolved_candidate.is_relative_to(root):
            return _issue(
                "user_profile_path_outside_root",
                "The user profile path is outside the configured context root.",
            )

        raw_or_issue = _read_profile_file(candidate)
        if isinstance(raw_or_issue, ContextSourceIssue):
            if raw_or_issue.code == "user_profile_file_missing":
                return ContextSourceResult()
            return ContextSourceResult(issues=[raw_or_issue])
        try:
            document = UserProfilesDocument.model_validate_json(raw_or_issue)
        except ValidationError:
            return _issue(
                "user_profile_invalid",
                "USER_PROFILES.json does not match user_profiles_v1.",
            )
        profile = document.profiles.get(request.user_id)
        if profile is None or not profile.attributes:
            return ContextSourceResult()

        content = json.dumps(
            {
                name: attribute.model_dump(mode="json", exclude_none=True)
                for name, attribute in profile.attributes.items()
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        requested_limit = request.section_char_budgets.get(
            USER_PROFILE_SOURCE_ID,
            USER_PROFILE_COMPILED_MAX_CHARS,
        )
        compiled_limit = min(
            USER_PROFILE_COMPILED_MAX_CHARS,
            max(0, requested_limit),
        )
        if len(content) > compiled_limit:
            return _issue(
                "user_profile_content_too_large",
                "The user profile exceeds the configured context budget.",
            )

        return ContextSourceResult(
            sections=[
                ContextSection(
                    section_id="user.profile",
                    kind="user_profile",
                    title="User profile",
                    content=content,
                    authority="user_profile_data",
                    stability="semi_stable",
                    source_type="editable_file",
                    source_ref=USER_PROFILE_SOURCE_REF,
                    source_version=f"revision:{profile.revision}",
                    identity_scope="user",
                    priority=20,
                    max_chars=compiled_limit,
                )
            ]
        )


def _read_profile_file(path: Path) -> bytes | ContextSourceIssue:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return _source_issue(
            "user_profile_file_missing",
            "USER_PROFILES.json is missing from the editable context root.",
        )
    except OSError:
        return _source_issue(
            "user_profile_file_unreadable",
            "USER_PROFILES.json could not be inspected.",
        )
    if stat.S_ISLNK(file_stat.st_mode):
        return _source_issue(
            "user_profile_symlink_not_allowed",
            "A symbolic link cannot be used as USER_PROFILES.json.",
        )
    if not stat.S_ISREG(file_stat.st_mode):
        return _source_issue(
            "user_profile_not_regular_file",
            "USER_PROFILES.json must be a regular file.",
        )
    if file_stat.st_size > USER_PROFILE_MAX_BYTES:
        return _source_issue(
            "user_profile_file_too_large",
            "USER_PROFILES.json exceeds the byte limit.",
        )

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return _source_issue(
            "user_profile_file_unreadable",
            "USER_PROFILES.json could not be read.",
        )
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            return _source_issue(
                "user_profile_not_regular_file",
                "USER_PROFILES.json must be a regular file.",
            )
        raw = os.read(descriptor, USER_PROFILE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > USER_PROFILE_MAX_BYTES:
        return _source_issue(
            "user_profile_file_too_large",
            "USER_PROFILES.json exceeds the byte limit.",
        )
    return raw


def _issue(code: str, public_message: str) -> ContextSourceResult:
    return ContextSourceResult(
        issues=[_source_issue(code, public_message)],
    )


def _source_issue(code: str, public_message: str) -> ContextSourceIssue:
    return ContextSourceIssue(
        code=code,
        source_ref=USER_PROFILE_SOURCE_REF,
        public_message=public_message,
    )
