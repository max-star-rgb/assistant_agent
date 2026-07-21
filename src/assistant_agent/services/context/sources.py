"""Governed context source contracts and per-run coordination."""

from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from assistant_agent.schemas.context import (
    ContextSection,
    ContextSourceIssue,
    ContextSourceResult,
)


class ContextSourceRequest(BaseModel):
    """Bounded non-secret inputs available to context sources."""

    user_id: str = Field(min_length=1)
    source_root: Path
    local_owner_user_id: str | None = None
    provider_mode: str = Field(min_length=1)
    editable_context_enabled: bool = False
    section_char_budgets: dict[str, int] = Field(default_factory=dict)
    enabled_source_ids: set[str] = Field(default_factory=set)


class ContextSource(Protocol):
    """Load validated context sections without runtime capabilities."""

    source_id: str

    def load(self, request: ContextSourceRequest) -> ContextSourceResult:
        raise NotImplementedError


class ContextSourceCoordinator:
    """Load enabled sources once and enforce cross-source invariants."""

    def __init__(
        self,
        sources: Iterable[ContextSource],
        *,
        max_issues: int = 16,
    ) -> None:
        self._sources = tuple(sources)
        self._max_issues = max(0, max_issues)

    def load_once(self, request: ContextSourceRequest) -> ContextSourceResult:
        """Return one frozen, prompt-safe result for an assistant run."""

        if not request.editable_context_enabled:
            return ContextSourceResult()

        sections: list[ContextSection] = []
        issues: list[ContextSourceIssue] = []
        used_last_known_good = False
        seen_ids: set[str] = set()
        soul_seen = False

        for source in self._sources:
            if source.source_id not in request.enabled_source_ids:
                continue
            try:
                loaded = source.load(request)
            except Exception:
                issues.append(
                    ContextSourceIssue(
                        code="context_source_load_failed",
                        source_ref=f"editable_context:{source.source_id}",
                        public_message="The editable context source could not be loaded.",
                    )
                )
                continue

            used_last_known_good = used_last_known_good or loaded.used_last_known_good
            issues.extend(loaded.issues)
            for section in loaded.sections:
                if not section.content.strip():
                    issues.append(
                        ContextSourceIssue(
                            code="context_source_empty_section_rejected",
                            source_ref=section.source_ref,
                            section_id=section.section_id,
                            public_message="An empty context section was rejected.",
                        )
                    )
                    continue
                if section.sensitive:
                    issues.append(
                        ContextSourceIssue(
                            code="context_source_sensitive_section_rejected",
                            source_ref=section.source_ref,
                            section_id=section.section_id,
                            public_message="A sensitive context section was rejected.",
                        )
                    )
                    continue
                if section.section_id in seen_ids or (section.kind == "soul" and soul_seen):
                    issues.append(
                        ContextSourceIssue(
                            code="context_source_duplicate_section_id",
                            source_ref=section.source_ref,
                            section_id=section.section_id,
                            public_message="A duplicate context section was rejected.",
                        )
                    )
                    continue
                seen_ids.add(section.section_id)
                soul_seen = soul_seen or section.kind == "soul"
                sections.append(section)

        return ContextSourceResult(
            sections=sections,
            issues=issues[: self._max_issues],
            used_last_known_good=used_last_known_good,
        )
