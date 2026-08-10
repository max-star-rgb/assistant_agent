from __future__ import annotations

import re

import pytest

from evals.release_review.contracts import ReleaseScenario
from evals.release_review.staging import (
    CleanupResult,
    PreparedStagingResource,
    StagingResourceManager,
)


class RecordingAdapter:
    def __init__(self, *, cleanup_result: CleanupResult | None = None) -> None:
        self.prepared: list[str] = []
        self.cleaned: list[tuple[str, tuple[str, ...]]] = []
        self.cleanup_result = cleanup_result

    def prepare(
        self, *, namespace: str, scenario: ReleaseScenario
    ) -> PreparedStagingResource:
        self.prepared.append(namespace)
        return PreparedStagingResource(
            runtime_metadata={"tenant": namespace, "scenario": scenario.id},
            resource_refs=(f"calendar://{namespace}/event-1",),
        )

    def cleanup(
        self, *, namespace: str, resource_refs: tuple[str, ...]
    ) -> CleanupResult:
        self.cleaned.append((namespace, resource_refs))
        return self.cleanup_result or CleanupResult(
            status="succeeded",
            resource_refs=resource_refs,
            cleaned_refs=resource_refs,
        )


def _scenario(
    *, profile: str = "test_calendar", cleanup: str = "required"
) -> ReleaseScenario:
    return ReleaseScenario.model_validate(
        {
            "id": "staging_calendar_probe",
            "phase": "staging",
            "capability": "calendar",
            "risk": "critical",
            "request": "create and read a test event",
            "tool_contract": {
                "required": ["calendar.create"],
                "allowed": ["calendar.read"],
                "forbidden": [],
            },
            "staging": {"resource_profile": profile, "cleanup": cleanup},
        }
    )


def test_namespace_is_safe_deterministic_and_exposed_as_runtime_metadata() -> None:
    adapter = RecordingAdapter()
    manager = StagingResourceManager({"test_calendar": adapter})

    first = manager.prepare("RC_2026.08.10", _scenario())
    second = manager.prepare("RC_2026.08.10", _scenario())

    assert first.namespace == second.namespace
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", first.namespace)
    assert first.runtime_metadata == {
        "release_review": {
            "namespace": first.namespace,
            "resource_profile": "test_calendar",
            "resource_refs": [f"calendar://{first.namespace}/event-1"],
        },
        "tenant": first.namespace,
        "scenario": "staging_calendar_probe",
    }


def test_cleanup_is_idempotent() -> None:
    adapter = RecordingAdapter()
    lease = StagingResourceManager({"test_calendar": adapter}).prepare(
        "release-1", _scenario()
    )

    first = lease.cleanup()
    second = lease.cleanup()

    assert first.status == "succeeded"
    assert second is first
    assert len(adapter.cleaned) == 1


def test_readonly_cleanup_is_skipped_without_calling_adapter() -> None:
    adapter = RecordingAdapter()
    lease = StagingResourceManager({"amap_readonly": adapter}).prepare(
        "release-1", _scenario(profile="amap_readonly", cleanup="skipped")
    )

    result = lease.cleanup()

    assert result.status == "skipped"
    assert result.infrastructure_status is None
    assert adapter.cleaned == []


def test_partial_cleanup_failure_retains_resource_references() -> None:
    failed = CleanupResult(
        status="failed",
        resource_refs=("calendar://namespace/event-1", "calendar://namespace/event-2"),
        cleaned_refs=("calendar://namespace/event-1",),
        failed_refs=("calendar://namespace/event-2",),
        issues=("permission denied",),
        infrastructure_status="cleanup_failed",
    )
    adapter = RecordingAdapter(cleanup_result=failed)
    lease = StagingResourceManager({"test_calendar": adapter}).prepare(
        "release-1", _scenario()
    )

    result = lease.cleanup()

    assert result.status == "failed"
    assert result.resource_refs == failed.resource_refs
    assert result.failed_refs == ("calendar://namespace/event-2",)
    assert result.infrastructure_status == "cleanup_failed"


def test_manager_rejects_unconfigured_profile_and_unsafe_release_id() -> None:
    manager = StagingResourceManager({})

    with pytest.raises(ValueError, match="not configured"):
        manager.prepare("release-1", _scenario())
    with pytest.raises(ValueError, match="release_id"):
        StagingResourceManager({"test_calendar": RecordingAdapter()}).prepare(
            "../production", _scenario()
        )
