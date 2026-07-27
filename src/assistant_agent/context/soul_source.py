"""Governed local SOUL.md context source."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import re
import secrets
import stat
from pathlib import Path

from assistant_agent.context.models import (
    ContextSection,
    ContextSourceIssue,
    ContextSourceResult,
)
from assistant_agent.context.sources import ContextSourceRequest


SOUL_SOURCE_ID = "soul"
SOUL_SOURCE_REF = "editable_context:soul"
SOUL_FILE_NAME = "SOUL.md"
SOUL_MAX_BYTES = 16_000
SOUL_MAX_CHARS = 4_000
SOUL_COMPILED_MAX_CHARS = 2_000
SOUL_SUBSECTION_MAX_CHARS = 800
SOUL_SECTION_ORDER = (
    "Relationship Boundaries",
    "Avoid",
    "Persona",
    "Expression Style",
)

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|bearer|cookie|secret(?:[_-]?token)?|token|password)\b"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_PREFIX_RE = re.compile(
    r"\b(?:sk|pk|qwen|dashscope)-[A-Za-z0-9._-]{4,}\b",
    re.IGNORECASE,
)
_BASE64_RE = re.compile(
    r"\b(?:[A-Za-z0-9+/]{80,}={0,2}|data:[^;\s]+;base64,[A-Za-z0-9+/=]{32,})\b"
)
_RAW_MARKERS = (
    "raw_provider_payload",
    "raw_provider_response",
    "provider_raw_response",
)


class SoulContextSource:
    """Load one owner-bound SOUL.md into a bounded context section."""

    source_id = SOUL_SOURCE_ID

    def __init__(self) -> None:
        self._version_key = secrets.token_bytes(32)
        self._last_known_good: dict[tuple[str, str], ContextSection] = {}

    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        """Load, validate, and compile the fixed SOUL file."""

        if not request.editable_context_enabled:
            return ContextSourceResult()
        owner_id = request.local_owner_user_id
        if not owner_id:
            return _result_with_issue(
                "editable_context_owner_unconfigured",
                "Editable context requires an explicitly bound local owner.",
            )
        if request.user_id != owner_id:
            return _result_with_issue(
                "editable_context_identity_mismatch",
                "Editable context is not available for this request identity.",
            )

        root = request.source_root.expanduser().resolve(strict=False)
        cache_key = (str(root), owner_id)
        candidate = root / SOUL_FILE_NAME
        resolved_candidate = candidate.resolve(strict=False)
        if not resolved_candidate.is_relative_to(root):
            return self._failed(
                cache_key,
                "soul_path_outside_root",
                "The SOUL source path is outside the configured context root.",
            )

        try:
            file_stat = candidate.lstat()
        except FileNotFoundError:
            return _result_with_issue(
                "soul_file_missing",
                "The configured SOUL source is missing.",
            )
        except OSError:
            return self._failed(
                cache_key,
                "soul_file_unreadable",
                "The configured SOUL source could not be inspected.",
            )

        if stat.S_ISLNK(file_stat.st_mode):
            return self._failed(
                cache_key,
                "soul_symlink_not_allowed",
                "A symbolic link cannot be used as the SOUL source.",
            )
        if not stat.S_ISREG(file_stat.st_mode):
            return self._failed(
                cache_key,
                "soul_not_regular_file",
                "The configured SOUL source is not a regular file.",
            )
        if file_stat.st_size > SOUL_MAX_BYTES:
            return self._failed(
                cache_key,
                "soul_file_too_large",
                "The configured SOUL source exceeds the byte limit.",
            )

        raw_or_issue = _read_bounded_file(candidate)
        if isinstance(raw_or_issue, ContextSourceIssue):
            return self._failed_issue(cache_key, raw_or_issue)
        raw = raw_or_issue
        if len(raw) > SOUL_MAX_BYTES:
            return self._failed(
                cache_key,
                "soul_file_too_large",
                "The configured SOUL source exceeds the byte limit.",
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._failed(
                cache_key,
                "soul_invalid_utf8",
                "The configured SOUL source is not valid UTF-8.",
            )
        if len(text) > SOUL_MAX_CHARS:
            return self._failed(
                cache_key,
                "soul_content_too_large",
                "The configured SOUL source exceeds the character limit.",
            )
        if _contains_unsafe_material(text):
            return self._failed(
                cache_key,
                "soul_unsafe_content",
                "The configured SOUL source contains unsafe material.",
            )

        parsed = _parse_sections(text)
        if isinstance(parsed, ContextSourceIssue):
            return self._failed_issue(cache_key, parsed)
        if not any(parsed.values()):
            return self._failed(
                cache_key,
                "soul_empty",
                "The configured SOUL source has no supported content.",
            )

        requested_limit = request.section_char_budgets.get(
            SOUL_SOURCE_ID,
            SOUL_COMPILED_MAX_CHARS,
        )
        compiled_limit = min(SOUL_COMPILED_MAX_CHARS, max(0, requested_limit))
        content, selected_count, omitted_count = _compile_sections(
            parsed,
            max_chars=compiled_limit,
        )
        if not content:
            return self._failed(
                cache_key,
                "soul_no_content_within_budget",
                "No complete SOUL paragraph fits the configured budget.",
            )

        source_version = hmac.new(
            self._version_key,
            raw,
            hashlib.sha256,
        ).hexdigest()
        previous = self._last_known_good.get(cache_key)
        notes = [
            f"selected_paragraphs:{selected_count}",
            f"omitted_paragraphs:{omitted_count}",
        ]
        if previous is None or previous.source_version != source_version:
            notes.append("source_version_changed")
        section = ContextSection(
            section_id="owner.soul",
            kind="soul",
            title="Owner persona",
            content=content,
            authority="owner_persona",
            stability="semi_stable",
            source_type="editable_file",
            source_ref=SOUL_SOURCE_REF,
            source_version=source_version,
            identity_scope="local_owner",
            priority=20,
            max_chars=compiled_limit,
            notes=notes,
        )
        self._last_known_good[cache_key] = section
        return ContextSourceResult(sections=[section])

    def _failed(
        self,
        cache_key: tuple[str, str],
        code: str,
        public_message: str,
    ) -> ContextSourceResult:
        return self._failed_issue(
            cache_key,
            ContextSourceIssue(
                code=code,
                source_ref=SOUL_SOURCE_REF,
                public_message=public_message,
            ),
        )

    def _failed_issue(
        self,
        cache_key: tuple[str, str],
        issue: ContextSourceIssue,
    ) -> ContextSourceResult:
        cached = self._last_known_good.get(cache_key)
        if cached is None:
            return ContextSourceResult(issues=[issue])
        notes = [
            note
            for note in cached.notes
            if note not in {"source_version_changed", "last_known_good"}
        ]
        notes.append("last_known_good")
        fallback = cached.model_copy(update={"notes": notes})
        return ContextSourceResult(
            sections=[fallback],
            issues=[issue],
            used_last_known_good=True,
        )


def _read_bounded_file(path: Path) -> bytes | ContextSourceIssue:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        code = "soul_symlink_not_allowed" if exc.errno == errno.ELOOP else "soul_file_unreadable"
        message = (
            "A symbolic link cannot be used as the SOUL source."
            if code == "soul_symlink_not_allowed"
            else "The configured SOUL source could not be read."
        )
        return ContextSourceIssue(
            code=code,
            source_ref=SOUL_SOURCE_REF,
            public_message=message,
        )

    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            return ContextSourceIssue(
                code="soul_not_regular_file",
                source_ref=SOUL_SOURCE_REF,
                public_message="The configured SOUL source is not a regular file.",
            )
        chunks: list[bytes] = []
        remaining = SOUL_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 8_192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return ContextSourceIssue(
            code="soul_file_unreadable",
            source_ref=SOUL_SOURCE_REF,
            public_message="The configured SOUL source could not be read.",
        )
    finally:
        os.close(fd)


def _contains_unsafe_material(text: str) -> bool:
    lowered = text.lower()
    return bool(
        _SECRET_ASSIGNMENT_RE.search(text)
        or _BEARER_RE.search(text)
        or _KEY_PREFIX_RE.search(text)
        or _BASE64_RE.search(text)
        or any(marker in lowered for marker in _RAW_MARKERS)
    )


def _parse_sections(
    text: str,
) -> dict[str, list[str]] | ContextSourceIssue:
    lines_by_heading: dict[str, list[str]] = {
        heading: [] for heading in SOUL_SECTION_ORDER
    }
    current_heading: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading not in lines_by_heading:
                return ContextSourceIssue(
                    code="soul_unknown_section",
                    source_ref=SOUL_SOURCE_REF,
                    public_message="The configured SOUL source contains an unsupported section.",
                )
            current_heading = heading
            continue
        if current_heading is None:
            if line.strip():
                return ContextSourceIssue(
                    code="soul_unknown_section",
                    source_ref=SOUL_SOURCE_REF,
                    public_message="The configured SOUL source contains content outside a supported section.",
                )
            continue
        lines_by_heading[current_heading].append(line.rstrip())

    return {
        heading: _paragraphs(lines)
        for heading, lines in lines_by_heading.items()
    }


def _paragraphs(lines: list[str]) -> list[str]:
    body = "\n".join(lines).strip()
    if not body:
        return []
    return [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    ]


def _compile_sections(
    parsed: dict[str, list[str]],
    *,
    max_chars: int,
) -> tuple[str, int, int]:
    selected: dict[str, list[str]] = {
        heading: [] for heading in SOUL_SECTION_ORDER
    }
    selected_count = 0
    omitted_count = 0
    for heading in SOUL_SECTION_ORDER:
        paragraphs = parsed.get(heading, [])
        for index, paragraph in enumerate(paragraphs):
            trial_subsection = [*selected[heading], paragraph]
            if len("\n\n".join(trial_subsection)) > SOUL_SUBSECTION_MAX_CHARS:
                omitted_count += len(paragraphs) - index
                break
            trial = {name: list(values) for name, values in selected.items()}
            trial[heading] = trial_subsection
            if len(_render_selected_sections(trial)) > max_chars:
                omitted_count += len(paragraphs) - index
                break
            selected = trial
            selected_count += 1
    return _render_selected_sections(selected), selected_count, omitted_count


def _render_selected_sections(selected: dict[str, list[str]]) -> str:
    blocks = [
        f"## {heading}\n" + "\n\n".join(selected[heading])
        for heading in SOUL_SECTION_ORDER
        if selected[heading]
    ]
    return "\n\n".join(blocks)


def _result_with_issue(code: str, public_message: str) -> ContextSourceResult:
    return ContextSourceResult(
        issues=[
            ContextSourceIssue(
                code=code,
                source_ref=SOUL_SOURCE_REF,
                public_message=public_message,
            )
        ]
    )
