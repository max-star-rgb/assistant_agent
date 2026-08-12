"""Background dispatcher for independently leased Workflow work items."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from assistant_agent.workflows.runtime import WorkflowRuntime
from assistant_agent.workflows.service import WorkflowService
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
        max_concurrent_items: int = 4,
    ) -> None:
        if max_concurrent_items < 1:
            raise ValueError("max_concurrent_items must be positive")
        self.service = service
        self.runtime = runtime
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.max_concurrent_items = max_concurrent_items

    def run_once(self) -> bool:
        claims = []
        advanced = False
        slot = 0
        probes_remaining = self.max_concurrent_items * 2
        while slot < self.max_concurrent_items and probes_remaining > 0:
            probes_remaining -= 1
            dispatch = self.service.store.claim_ready_work_item(
                worker_id=f"{self.worker_id}:{slot}",
                now=self.runtime.clock(),
                lease_seconds=self.lease_seconds,
                model_call_limit=self.runtime.model_call_limit_per_item,
                tool_call_limit=self.runtime.tool_call_limit_per_item,
            )
            if dispatch is None:
                break
            advanced = True
            if dispatch.lease is None:
                continue
            claims.append(dispatch)
            slot += 1
        if not claims:
            return advanced
        with ThreadPoolExecutor(
            max_workers=len(claims),
            thread_name_prefix="durable-workflow",
        ) as executor:
            futures = [
                executor.submit(
                    self.runtime.run_claim,
                    claim,
                    lease_seconds=self.lease_seconds,
                )
                for claim in claims
            ]
            for future in futures:
                future.result()
        return True

    def run(self, stop_event: Event) -> None:
        while not stop_event.is_set():
            try:
                advanced = self.run_once()
            except Exception:
                logger.exception(
                    "Durable Workflow work item failed; lease recovery will retry."
                )
                stop_event.wait(self.poll_seconds)
                continue
            if not advanced:
                stop_event.wait(self.poll_seconds)
