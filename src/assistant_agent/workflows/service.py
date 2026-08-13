"""Identity-scoped durable workflow service."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable

from assistant_agent.identity import RequestIdentity
from assistant_agent.workflows.definitions import (
    WorkflowDefinitionCatalog,
    build_bootstrap_plan,
)
from assistant_agent.workflows.models import WorkflowBundle, WorkflowSubmission, utc_now
from assistant_agent.workflows.store import (
    WorkflowAlreadyExists,
    WorkflowStore,
)
from assistant_agent.workflows.transitions import (
    WorkflowLimits,
    create_initial_bundle,
    normalize_budget,
)


class WorkflowServiceError(RuntimeError):
    code = "workflow_service_error"


class WorkflowNotFound(WorkflowServiceError):
    code = "workflow_not_found"


class WorkflowAccessDenied(WorkflowServiceError):
    code = "workflow_access_denied"


class WorkflowSubmissionRejected(WorkflowServiceError):
    code = "workflow_submission_rejected"


class WorkflowSubmissionConflict(WorkflowServiceError):
    code = "workflow_submission_conflict"


def submission_digest(submission: WorkflowSubmission) -> str:
    payload = json.dumps(
        submission.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class WorkflowService:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        definitions: WorkflowDefinitionCatalog,
        limits: WorkflowLimits | None = None,
        clock: Callable = utc_now,
    ) -> None:
        self.store = store
        self.definitions = definitions
        self.limits = limits or WorkflowLimits()
        self.clock = clock

    def submit(
        self,
        *,
        identity: RequestIdentity,
        ingress_run_id: str,
        submission: WorkflowSubmission,
        ingress_trace_id: str | None = None,
        ingress_parent_span_id: str | None = None,
    ) -> WorkflowBundle:
        if identity.session_id is None or not ingress_run_id:
            raise WorkflowSubmissionRejected(
                "trusted session and run identity are required"
            )
        digest = submission_digest(submission)
        existing = self._load_submission(identity, ingress_run_id, submission)
        if existing is not None:
            return self._resolve_duplicate(existing, digest)
        try:
            definition = self.definitions.require(submission.workflow_type)
            definition.validate_submission(submission)
            workflow_id = f"workflow_{secrets.token_hex(16)}"
            plan = build_bootstrap_plan(
                workflow_id=workflow_id,
                descriptor=definition.descriptor,
            )
            now = self.clock()
            bundle, events = create_initial_bundle(
                workflow_id=workflow_id,
                workflow_type=submission.workflow_type,
                definition_version=definition.descriptor.definition_version,
                user_id=identity.user_id,
                agent_id=identity.agent_id,
                session_id=identity.session_id,
                ingress_run_id=ingress_run_id,
                ingress_trace_id=ingress_trace_id,
                ingress_parent_span_id=ingress_parent_span_id,
                idempotency_key=submission.idempotency_key,
                submission_digest=digest,
                objective=submission.objective,
                deliverables=list(submission.deliverables),
                constraints=list(submission.constraints),
                inputs=dict(submission.inputs),
                seed_artifact_refs=list(submission.seed_artifact_refs),
                budget=normalize_budget(
                    submission.requested_budget,
                    limits=self.limits,
                    now=now,
                ),
                plan=plan,
                limits=self.limits,
                now=now,
                execution_engine="langgraph_v3",
            )
            return self.store.create(bundle, events)
        except WorkflowAlreadyExists:
            duplicate = self._load_submission(identity, ingress_run_id, submission)
            if duplicate is None:
                raise WorkflowSubmissionConflict("workflow submission raced")
            return self._resolve_duplicate(duplicate, digest)
        except WorkflowServiceError:
            raise
        except Exception as exc:
            raise WorkflowSubmissionRejected(
                "workflow submission was rejected"
            ) from exc

    def get_workflow(
        self, *, identity: RequestIdentity, workflow_id: str
    ) -> WorkflowBundle:
        bundle = self.store.load(workflow_id)
        if bundle is None:
            raise WorkflowNotFound(workflow_id)
        if (
            bundle.workflow.user_id != identity.user_id
            or bundle.workflow.agent_id != identity.agent_id
        ):
            raise WorkflowAccessDenied(workflow_id)
        return bundle

    def list_events(
        self,
        *,
        identity: RequestIdentity,
        workflow_id: str,
        after: int = 0,
        limit: int = 100,
    ):
        self.get_workflow(identity=identity, workflow_id=workflow_id)
        return self.store.list_events(
            workflow_id,
            after=max(0, after),
            limit=min(max(1, limit), 500),
        )

    def _load_submission(
        self,
        identity: RequestIdentity,
        ingress_run_id: str,
        submission: WorkflowSubmission,
    ) -> WorkflowBundle | None:
        return self.store.load_by_submission(
            user_id=identity.user_id,
            agent_id=identity.agent_id,
            ingress_run_id=ingress_run_id,
            idempotency_key=submission.idempotency_key,
        )

    @staticmethod
    def _resolve_duplicate(bundle: WorkflowBundle, digest: str) -> WorkflowBundle:
        if bundle.workflow.submission_digest != digest:
            raise WorkflowSubmissionConflict("idempotency key was reused")
        return bundle
