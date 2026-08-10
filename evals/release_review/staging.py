from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Mapping
from copy import deepcopy
from threading import Lock
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import ReleaseScenario


ALLOWED_STAGING_PROFILES = frozenset(
    {"deep_research_workflow", "amap_readonly", "test_calendar"}
)
_RELEASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PreparedStagingResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_metadata: dict[str, Any]
    resource_refs: tuple[str, ...] = ()


class CleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["succeeded", "failed", "skipped"]
    resource_refs: tuple[str, ...] = ()
    cleaned_refs: tuple[str, ...] = ()
    failed_refs: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    infrastructure_status: str | None = None


class StagingResourceAdapter(Protocol):
    def prepare(
        self,
        *,
        namespace: str,
        scenario: ReleaseScenario,
    ) -> PreparedStagingResource: ...

    def cleanup(
        self,
        *,
        namespace: str,
        resource_refs: tuple[str, ...],
    ) -> CleanupResult: ...


class StagingLease:
    def __init__(
        self,
        *,
        namespace: str,
        resource_profile: str,
        prepared: PreparedStagingResource,
        cleanup_policy: Literal["required", "skipped"],
        adapter: StagingResourceAdapter,
    ) -> None:
        self.namespace = namespace
        self.resource_profile = resource_profile
        self.resource_refs = prepared.resource_refs
        runtime_metadata = deepcopy(prepared.runtime_metadata)
        runtime_metadata["release_review"] = {
            "namespace": namespace,
            "resource_profile": resource_profile,
            "resource_refs": list(prepared.resource_refs),
        }
        self.runtime_metadata = runtime_metadata
        self._cleanup_policy = cleanup_policy
        self._adapter = adapter
        self._cleanup_result: CleanupResult | None = None
        self._cleanup_lock = Lock()

    def cleanup(self) -> CleanupResult:
        with self._cleanup_lock:
            if self._cleanup_result is not None:
                return self._cleanup_result
            if self._cleanup_policy == "skipped":
                self._cleanup_result = CleanupResult(
                    status="skipped",
                    resource_refs=self.resource_refs,
                )
                return self._cleanup_result
            try:
                result = self._adapter.cleanup(
                    namespace=self.namespace,
                    resource_refs=self.resource_refs,
                )
            except Exception as exc:
                result = CleanupResult(
                    status="failed",
                    resource_refs=self.resource_refs,
                    failed_refs=self.resource_refs,
                    issues=(f"{type(exc).__name__}: {exc}",),
                    infrastructure_status="cleanup_failed",
                )
            self._cleanup_result = result
            return result


class StagingResourceManager:
    def __init__(self, adapters: Mapping[str, StagingResourceAdapter]) -> None:
        unknown = set(adapters) - ALLOWED_STAGING_PROFILES
        if unknown:
            raise ValueError(f"unsupported staging profiles: {', '.join(sorted(unknown))}")
        self._adapters = dict(adapters)

    def prepare(self, release_id: str, scenario: ReleaseScenario) -> StagingLease:
        if not _RELEASE_ID_PATTERN.fullmatch(release_id):
            raise ValueError("release_id must be a safe identifier")
        if scenario.phase != "staging" or scenario.staging is None:
            raise ValueError("staging resource preparation requires a staging scenario")
        profile = scenario.staging.resource_profile
        adapter = self._adapters.get(profile)
        if adapter is None:
            raise ValueError(f"staging resource profile {profile!r} is not configured")
        namespace = _staging_namespace(release_id, scenario.id)
        prepared = adapter.prepare(namespace=namespace, scenario=scenario)
        return StagingLease(
            namespace=namespace,
            resource_profile=profile,
            prepared=prepared,
            cleanup_policy=scenario.staging.cleanup,
            adapter=adapter,
        )


class LocalStagingProfileAdapter:
    """Prepare operation-scoped local stores for workflow and calendar staging."""

    def __init__(self, profile: str, root: str | Any) -> None:
        from pathlib import Path

        if profile not in ALLOWED_STAGING_PROFILES:
            raise ValueError(f"unsupported staging profile: {profile}")
        self.profile = profile
        self.root = Path(root).resolve()

    def prepare(
        self,
        *,
        namespace: str,
        scenario: ReleaseScenario,
    ) -> PreparedStagingResource:
        del scenario
        if self.profile == "amap_readonly":
            return PreparedStagingResource(runtime_metadata={})
        target = (self.root / namespace).resolve()
        if self.root not in target.parents:
            raise ValueError("staging target escaped configured root")
        target.mkdir(parents=True, exist_ok=True)
        if self.profile == "deep_research_workflow":
            metadata = {
                "staging_paths": {
                    "workflow_db": str(target / "workflows.sqlite3"),
                    "workflow_artifacts": str(target / "artifacts"),
                }
            }
        else:
            metadata = {
                "staging_paths": {"calendar_db": str(target / "calendar.sqlite3")}
            }
        return PreparedStagingResource(
            runtime_metadata=metadata,
            resource_refs=(f"file://{target}",),
        )

    def cleanup(
        self,
        *,
        namespace: str,
        resource_refs: tuple[str, ...],
    ) -> CleanupResult:
        target = (self.root / namespace).resolve()
        if self.root not in target.parents:
            return CleanupResult(
                status="failed",
                resource_refs=resource_refs,
                failed_refs=resource_refs,
                issues=("staging target escaped configured root",),
                infrastructure_status="cleanup_failed",
            )
        if target.exists():
            shutil.rmtree(target)
        return CleanupResult(
            status="succeeded",
            resource_refs=resource_refs,
            cleaned_refs=resource_refs,
        )


def _staging_namespace(release_id: str, scenario_id: str) -> str:
    source = f"{release_id}-{scenario_id}"
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:8]
    return f"rr-{slug[:49].rstrip('-')}-{digest}"
