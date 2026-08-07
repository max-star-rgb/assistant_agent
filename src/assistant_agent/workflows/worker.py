"""Background worker for bounded workflow quanta."""

from __future__ import annotations

import logging
from threading import Event

from assistant_agent.workflows.models import utc_now
from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.service import WorkflowService
from assistant_agent.workflows.store import WorkflowLeaseConflict, WorkflowRevisionConflict


logger = logging.getLogger(__name__)


class DurableWorkflowWorker:
    def __init__(
        self,
        *,
        service: WorkflowService,
        runtime: WorkflowRuntime,
        worker_id: str,
        lease_seconds: int = 30,
        poll_seconds: float = 1.0,
    ) -> None:
        self.service = service
        self.runtime = runtime
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds

    def run_once(self) -> bool:
        lease = self.service.store.claim_next(
            worker_id=self.worker_id,
            now=utc_now(),
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return False
        saved = self.runtime.run_quantum(lease)
        try:
            self.service.store.release(
                lease,
                expected_revision=saved.workflow.revision,
            )
        except (WorkflowLeaseConflict, WorkflowRevisionConflict):
            pass
        return True

    def run(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            try:
                advanced = self.run_once()
            except Exception:
                logger.exception("Durable Workflow quantum failed; lease recovery will retry.")
                stop_event.wait(self.poll_seconds)
                continue
            if not advanced:
                stop_event.wait(self.poll_seconds)
