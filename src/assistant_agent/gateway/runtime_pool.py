"""Bounded runtime instance pool for Gateway backend execution."""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition
from typing import Any

from assistant_agent.runtime.runtime import AgentGraphRuntime
from assistant_agent.identity import RequestIdentity
from assistant_agent.runtime.requests import UserRequest
from assistant_agent.runtime.assistant_run_service import (
    AssistantRunArtifacts,
    create_runtime,
    run_assistant_request,
)

RuntimeFactory = Callable[[], AgentGraphRuntime]
RuntimeCleanup = Callable[[AgentGraphRuntime], None]
RunRequest = Callable[..., AssistantRunArtifacts]


class GatewayRuntimePool:
    """Lazily create and reuse a bounded number of runtime instances."""

    def __init__(
        self,
        *,
        max_runtime_instances: int,
        runtime_factory: RuntimeFactory | None = None,
        run_request: RunRequest | None = None,
        runtime_cleanup: RuntimeCleanup | None = None,
    ) -> None:
        if (
            isinstance(max_runtime_instances, bool)
            or not isinstance(max_runtime_instances, int)
            or max_runtime_instances <= 0
        ):
            raise ValueError("max_runtime_instances must be a positive integer")
        self.max_runtime_instances = max_runtime_instances
        self._runtime_factory = runtime_factory or create_runtime
        self._run_request = run_request or run_assistant_request
        self._runtime_cleanup = runtime_cleanup
        self._condition = Condition()
        self._runtimes: list[AgentGraphRuntime] = []
        self._idle: list[AgentGraphRuntime] = []
        self._closed = False

    @property
    def created_count(self) -> int:
        with self._condition:
            return len(self._runtimes)

    @property
    def idle_count(self) -> int:
        with self._condition:
            return len(self._idle)

    def run_request(self, request: UserRequest, **kwargs: Any) -> AssistantRunArtifacts:
        runtime = self._checkout()
        try:
            return self._run_request(request, runtime=runtime, **kwargs)
        finally:
            self._checkin(runtime)

    def initialize_session_memory(self, identity: RequestIdentity) -> None:
        """Warm one session snapshot outside the first turn lifecycle."""

        runtime = self._checkout()
        try:
            runtime.initialize_session_memory(identity)
        finally:
            self._checkin(runtime)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            runtimes = list(self._runtimes)
            self._idle.clear()
            self._condition.notify_all()
        if self._runtime_cleanup is None:
            return
        seen: set[int] = set()
        for runtime in runtimes:
            identity = id(runtime)
            if identity in seen:
                continue
            seen.add(identity)
            self._runtime_cleanup(runtime)

    def _checkout(self) -> AgentGraphRuntime:
        with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError("gateway_runtime_pool_closed")
                if self._idle:
                    return self._idle.pop()
                if len(self._runtimes) < self.max_runtime_instances:
                    runtime = self._runtime_factory()
                    self._runtimes.append(runtime)
                    return runtime
                self._condition.wait()

    def _checkin(self, runtime: AgentGraphRuntime) -> None:
        with self._condition:
            if not self._closed:
                self._idle.append(runtime)
            self._condition.notify()


def shared_gateway_runtime_factory(primary_factory: RuntimeFactory) -> RuntimeFactory:
    """Return a factory that shares process-owned stores with the primary runtime."""

    primary_runtime: AgentGraphRuntime | None = None
    primary_returned = False

    def create() -> AgentGraphRuntime:
        nonlocal primary_runtime, primary_returned
        if primary_runtime is None:
            primary_runtime = primary_factory()
        if not primary_returned:
            primary_returned = True
            return primary_runtime
        return AgentGraphRuntime(
            config=primary_runtime.config,
            agent_id=primary_runtime.agent_id,
            long_term_memory_service=primary_runtime.long_term_memory_service,
            session_store=primary_runtime.session_store,
            trace_store=primary_runtime.trace_store,
            video_context_store=primary_runtime.video_context_store,
            realtime_video_memory_store=primary_runtime.realtime_video_memory_store,
            durable_task_service=primary_runtime.durable_task_service,
        )

    return create
