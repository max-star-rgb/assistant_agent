"""Bounded workflow context manifests."""

from __future__ import annotations

from typing import Protocol

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
    total_excerpt_tokens: int = Field(default=0, ge=0)
    token_budget: int = Field(default=0, ge=0)
    tokenizer_id: str = "workflow-estimator-v1"
    trimmed: bool = False


class WorkflowTokenCounter(Protocol):
    tokenizer_id: str

    def count_text(self, value: str) -> int: ...


class EstimatedWorkflowTokenCounter:
    """Offline fallback used when no model tokenizer is configured."""

    tokenizer_id = "workflow-estimator-v1"

    def count_text(self, value: str) -> int:
        ascii_count = sum(ord(character) < 128 for character in value)
        non_ascii_count = len(value) - ascii_count
        return non_ascii_count + (ascii_count + 3) // 4


_STAGE_TOKEN_BUDGETS = {
    "scope": 16_000,
    "collect_sources": 32_000,
    "extract_evidence": 96_000,
    "outline": 96_000,
    "draft": 192_000,
    "verify": 192_000,
    "synthesize": 256_000,
    "deliver": 256_000,
}


class WorkflowContextCompiler:
    def __init__(
        self,
        *,
        artifact_store: LocalWorkflowArtifactStore,
        token_counter: WorkflowTokenCounter | None = None,
        model_context_window_tokens: int = 128_000,
        output_reserve_tokens: int = 8_192,
        safety_margin_tokens: int = 0,
        max_window_fraction: float = 0.25,
        stage_token_budgets: dict[str, int] | None = None,
        max_total_chars: int | None = None,
        max_artifact_chars: int | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.token_counter = token_counter or EstimatedWorkflowTokenCounter()
        self.model_context_window_tokens = max(8_192, model_context_window_tokens)
        self.output_reserve_tokens = max(0, output_reserve_tokens)
        self.safety_margin_tokens = max(0, safety_margin_tokens)
        self.max_window_fraction = min(1.0, max(0.01, max_window_fraction))
        self.stage_token_budgets = {
            **_STAGE_TOKEN_BUDGETS,
            **(stage_token_budgets or {}),
        }
        self.max_total_chars = (
            None if max_total_chars is None else max(0, max_total_chars)
        )
        self.max_artifact_chars = (
            None if max_artifact_chars is None else max(0, max_artifact_chars)
        )

    def compile(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        objective: str,
        constraints: list[str],
        artifact_refs: list[str],
        work_item_kind: str = "generic",
    ) -> WorkflowContextManifest:
        token_budget = self._token_budget(work_item_kind)
        remaining_tokens = token_budget
        remaining_chars = self.max_total_chars
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
            token_allowance = remaining_tokens // artifacts_remaining
            excerpt = _truncate_to_token_budget(
                full_text,
                max_tokens=token_allowance,
                token_counter=self.token_counter,
            )
            if remaining_chars is not None:
                char_allowance = remaining_chars // artifacts_remaining
                if self.max_artifact_chars is not None:
                    char_allowance = min(char_allowance, self.max_artifact_chars)
                excerpt = excerpt[:char_allowance]
            elif self.max_artifact_chars is not None:
                excerpt = excerpt[:self.max_artifact_chars]
            if len(excerpt) < len(full_text):
                trimmed = True
            excerpts.append(WorkflowArtifactExcerpt(
                artifact_ref=ref.uri,
                kind=ref.kind,
                digest=ref.digest,
                excerpt=excerpt,
            ))
            excerpt_tokens = self.token_counter.count_text(excerpt)
            remaining_tokens -= excerpt_tokens
            if remaining_chars is not None:
                remaining_chars -= len(excerpt)
            if remaining_tokens <= 0 or remaining_chars == 0:
                trimmed = trimmed or len(excerpts) < len(artifact_refs)
                break
        total_tokens = sum(
            self.token_counter.count_text(item.excerpt) for item in excerpts
        )
        return WorkflowContextManifest(
            workflow_id=workflow_id,
            objective=objective,
            constraints=list(constraints),
            artifacts=excerpts,
            total_excerpt_chars=sum(len(item.excerpt) for item in excerpts),
            total_excerpt_tokens=total_tokens,
            token_budget=token_budget,
            tokenizer_id=self.token_counter.tokenizer_id,
            trimmed=trimmed,
        )

    def _token_budget(self, work_item_kind: str) -> int:
        stage_budget = max(
            1,
            self.stage_token_budgets.get(work_item_kind, 128_000),
        )
        fractional_budget = int(
            self.model_context_window_tokens * self.max_window_fraction
        )
        available = max(
            1,
            self.model_context_window_tokens
            - self.output_reserve_tokens
            - self.safety_margin_tokens,
        )
        return min(stage_budget, fractional_budget, available)


def _truncate_to_token_budget(
    text: str,
    *,
    max_tokens: int,
    token_counter: WorkflowTokenCounter,
) -> str:
    if max_tokens <= 0:
        return ""
    if token_counter.count_text(text) <= max_tokens:
        return text
    low = 0
    high = len(text)
    while low < high:
        midpoint = (low + high + 1) // 2
        if token_counter.count_text(text[:midpoint]) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    return text[:low]
