"""Run-scoped, metadata-only state for website exploration sessions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
import secrets
import threading
import time
from typing import Callable, Literal


BrowserActionName = Literal["inspect", "click", "back", "wait"]
BrowserElementKind = Literal["navigate", "expand"]
_ALLOWED_ACTIONS = frozenset({"inspect", "click", "back", "wait"})
_ALLOWED_ELEMENT_KINDS = frozenset({"navigate", "expand"})
_ELEMENT_REF_PATTERN = re.compile(r"^e[1-9][0-9]*$")
MAX_SNAPSHOT_ELEMENTS = 40
DEFAULT_MAX_RECORDS = 256
DEFAULT_MAX_RECORDS_PER_RUN = 8


@dataclass(frozen=True)
class BrowserElementDescriptor:
    """Bounded safe-action identity for one ref in a displayed snapshot."""

    ref: str
    kind: BrowserElementKind
    role: str
    name: str
    href: str | None = None
    node_id: str | None = None

    def __post_init__(self) -> None:
        if not _ELEMENT_REF_PATTERN.fullmatch(self.ref):
            raise ValueError("invalid element_ref")
        if self.kind not in _ALLOWED_ELEMENT_KINDS:
            raise ValueError("invalid element kind")
        if not self.role or len(self.role) > 100:
            raise ValueError("invalid element role")
        if not self.name or len(self.name) > 1_000:
            raise ValueError("invalid element name")
        if self.kind == "navigate":
            if not self.href or len(self.href) > 2_000:
                raise ValueError("navigate descriptor requires bounded href")
            if self.node_id is not None:
                raise ValueError("navigate descriptor cannot have node_id")
        else:
            if self.href is not None:
                raise ValueError("expand descriptor cannot have href")
            if not self.node_id or len(self.node_id) > 256:
                raise ValueError("expand descriptor requires stable node_id")


@dataclass(frozen=True)
class BrowserExplorationAction:
    """A safe, reference-based action taken in a browser exploration session."""

    action: BrowserActionName
    element_ref: str | None
    snapshot_version: int
    selected_element: BrowserElementDescriptor | None = None

    def __post_init__(self) -> None:
        if self.action not in _ALLOWED_ACTIONS:
            raise ValueError("unsupported browser action")
        if (self.action == "click") != (self.element_ref is not None):
            raise ValueError("element_ref is required only for click")
        if (self.action == "click") != (self.selected_element is not None):
            raise ValueError("selected_element is required only for click")
        if self.element_ref is not None and not _ELEMENT_REF_PATTERN.fullmatch(
            self.element_ref
        ):
            raise ValueError("invalid element_ref")
        if (
            self.selected_element is not None
            and self.selected_element.ref != self.element_ref
        ):
            raise ValueError("selected_element ref mismatch")
        if not _is_snapshot_version(self.snapshot_version):
            raise ValueError("invalid snapshot_version")


@dataclass(frozen=True)
class BrowserExplorationRecord:
    """The complete allowed browser metadata associated with one opaque ID."""

    browser_session_id: str
    run_id: str
    session_id: str
    start_url: str
    snapshot_url: str
    snapshot_version: int
    snapshot_elements: tuple[BrowserElementDescriptor, ...] = ()
    actions: tuple[BrowserExplorationAction, ...] = ()


@dataclass(frozen=True)
class _StoredRecord:
    record: BrowserExplorationRecord
    expires_at: float


class BrowserExplorationStore:
    """A locked, expiring store that never retains browser secrets or content."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_records_per_run: int = DEFAULT_MAX_RECORDS_PER_RUN,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not _is_positive_int(max_records):
            raise ValueError("max_records must be a positive integer")
        if not _is_positive_int(max_records_per_run):
            raise ValueError("max_records_per_run must be a positive integer")
        if max_records_per_run > max_records:
            raise ValueError("max_records_per_run cannot exceed max_records")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._max_records = max_records
        self._max_records_per_run = max_records_per_run
        self._lock = threading.RLock()
        self._records: dict[str, _StoredRecord] = {}

    def create(
        self,
        *,
        run_id: str,
        session_id: str,
        start_url: str,
        snapshot_url: str | None = None,
        snapshot_version: int = 0,
        snapshot_elements: tuple[BrowserElementDescriptor, ...] = (),
    ) -> BrowserExplorationRecord:
        """Create an opaque exploration identifier owned by one run and session."""

        _validate_owner(run_id, session_id)
        if not isinstance(start_url, str) or not start_url:
            raise ValueError("start_url must be a non-empty string")
        displayed_url = start_url if snapshot_url is None else snapshot_url
        if not isinstance(displayed_url, str) or not displayed_url:
            raise ValueError("snapshot_url must be a non-empty string")
        if not _is_snapshot_version(snapshot_version):
            raise ValueError("invalid snapshot_version")

        with self._lock:
            self._prune_expired_locked()
            if len(self._records) >= self._max_records:
                raise ValueError("global_session_limit_exceeded")
            run_records = sum(
                stored.record.run_id == run_id for stored in self._records.values()
            )
            if run_records >= self._max_records_per_run:
                raise ValueError("run_session_limit_exceeded")
            browser_session_id = self._new_browser_session_id()
            record = BrowserExplorationRecord(
                browser_session_id=browser_session_id,
                run_id=run_id,
                session_id=session_id,
                start_url=start_url,
                snapshot_url=displayed_url,
                snapshot_version=snapshot_version,
                snapshot_elements=_validate_snapshot_elements(snapshot_elements),
            )
            self._records[browser_session_id] = _StoredRecord(
                record=record,
                expires_at=self._clock() + self._ttl_seconds,
            )
            return record

    def get_owned(
        self,
        browser_session_id: str,
        *,
        run_id: str,
        session_id: str,
    ) -> BrowserExplorationRecord | None:
        """Return a live record only when both owner identifiers match exactly."""

        with self._lock:
            self._prune_expired_locked()
            stored = self._get_live_owned(
                browser_session_id,
                run_id=run_id,
                session_id=session_id,
            )
            return stored.record if stored is not None else None

    def append_action(
        self,
        browser_session_id: str,
        *,
        run_id: str,
        session_id: str,
        action: BrowserActionName,
        element_ref: str | None,
        snapshot_version: int,
        selected_element: BrowserElementDescriptor | None = None,
        snapshot_url: str | None = None,
        snapshot_elements: tuple[BrowserElementDescriptor, ...] = (),
    ) -> BrowserExplorationRecord | None:
        """Append one safe action when its opaque ID belongs to this run/session."""

        exploration_action = BrowserExplorationAction(
            action=action,
            element_ref=element_ref,
            snapshot_version=snapshot_version,
            selected_element=selected_element,
        )
        with self._lock:
            self._prune_expired_locked()
            stored = self._get_live_owned(
                browser_session_id,
                run_id=run_id,
                session_id=session_id,
            )
            if stored is None:
                return None
            displayed_url = (
                stored.record.snapshot_url if snapshot_url is None else snapshot_url
            )
            if not isinstance(displayed_url, str) or not displayed_url:
                raise ValueError("snapshot_url must be a non-empty string")
            record = replace(
                stored.record,
                snapshot_version=snapshot_version,
                snapshot_url=displayed_url,
                snapshot_elements=_validate_snapshot_elements(snapshot_elements),
                actions=stored.record.actions + (exploration_action,),
            )
            self._records[browser_session_id] = replace(stored, record=record)
            return record

    def delete_run(self, run_id: str) -> int:
        """Delete every stored record owned by exactly one completed run."""

        with self._lock:
            self._prune_expired_locked()
            record_ids = [
                browser_session_id
                for browser_session_id, stored in self._records.items()
                if stored.record.run_id == run_id
            ]
            for browser_session_id in record_ids:
                del self._records[browser_session_id]
            return len(record_ids)

    def _new_browser_session_id(self) -> str:
        browser_session_id = secrets.token_urlsafe(24)
        while browser_session_id in self._records:
            browser_session_id = secrets.token_urlsafe(24)
        return browser_session_id

    def _get_live_owned(
        self,
        browser_session_id: str,
        *,
        run_id: str,
        session_id: str,
    ) -> _StoredRecord | None:
        stored = self._records.get(browser_session_id)
        if stored is None:
            return None
        if self._clock() >= stored.expires_at:
            del self._records[browser_session_id]
            return None
        if stored.record.run_id != run_id or stored.record.session_id != session_id:
            return None
        return stored

    def _prune_expired_locked(self) -> None:
        now = self._clock()
        expired = [
            browser_session_id
            for browser_session_id, stored in self._records.items()
            if now >= stored.expires_at
        ]
        for browser_session_id in expired:
            del self._records[browser_session_id]


def _validate_owner(run_id: str, session_id: str) -> None:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")


def _is_snapshot_version(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_snapshot_elements(
    elements: tuple[BrowserElementDescriptor, ...],
) -> tuple[BrowserElementDescriptor, ...]:
    if not isinstance(elements, tuple) or len(elements) > MAX_SNAPSHOT_ELEMENTS:
        raise ValueError("invalid snapshot elements")
    refs = [element.ref for element in elements]
    if len(refs) != len(set(refs)):
        raise ValueError("duplicate snapshot element ref")
    return elements
