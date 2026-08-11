"""Bounded workflow context manifests."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.artifacts import LocalWorkflowArtifactStore


class WorkflowArtifactExcerpt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ref: str
    kind: str
    digest: str
    excerpt: str


class WorkflowContextManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    objective: str
    constraints: list[str] = Field(default_factory=list)
    artifacts: list[WorkflowArtifactExcerpt] = Field(default_factory=list)
    total_excerpt_chars: int = Field(ge=0)
    trimmed: bool = False


class WorkflowContextCompiler:
    def __init__(
        self,
        *,
        artifact_store: LocalWorkflowArtifactStore,
        max_total_chars: int = 12_000,
        max_artifact_chars: int = 12_000,
    ) -> None:
        self.artifact_store = artifact_store
        self.max_total_chars = max(0, max_total_chars)
        self.max_artifact_chars = max(0, max_artifact_chars)

    def compile(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        objective: str,
        constraints: list[str],
        artifact_refs: list[str],
    ) -> WorkflowContextManifest:
        remaining = self.max_total_chars
        excerpts: list[WorkflowArtifactExcerpt] = []
        trimmed = False
        total_artifacts = len(artifact_refs)
        for index, artifact_ref in enumerate(artifact_refs):
            ref = self.artifact_store.get_ref(
                identity=identity,
                artifact_ref=artifact_ref,
            )
            if ref.workflow_id != workflow_id:
                raise ValueError("artifact does not belong to workflow")
            full_text = self.artifact_store.read_text(
                identity=identity,
                artifact_ref=artifact_ref,
            )
            artifacts_remaining = total_artifacts - index
            fair_share = remaining // artifacts_remaining
            allowance = min(self.max_artifact_chars, fair_share)
            excerpt = full_text[:allowance]
            if len(excerpt) < len(full_text):
                trimmed = True
            excerpts.append(WorkflowArtifactExcerpt(
                artifact_ref=ref.uri,
                kind=ref.kind,
                digest=ref.digest,
                excerpt=excerpt,
            ))
            remaining -= len(excerpt)
            if remaining <= 0:
                trimmed = trimmed or len(excerpts) < len(artifact_refs)
                break
        return WorkflowContextManifest(
            workflow_id=workflow_id,
            objective=objective,
            constraints=list(constraints),
            artifacts=excerpts,
            total_excerpt_chars=sum(len(item.excerpt) for item in excerpts),
            trimmed=trimmed,
        )
