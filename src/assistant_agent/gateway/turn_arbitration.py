"""Bounded control-plane execution for realtime turn arbitration."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from assistant_agent.gateway.turn_arbitration_models import (
    RealtimeTurnArbitrationDecision,
    RealtimeTurnArbitrationRequest,
    normalize_arbitration_decision,
    uncertain_arbitration_decision,
)
from assistant_agent.gateway.realtime_turn_arbiter import RealtimeTurnArbiter


GatewayTurnArbitrationStatus = Literal[
    "completed",
    "disabled",
    "timeout",
    "saturated",
    "failed",
]


@dataclass(frozen=True)
class GatewayTurnArbitrationPolicy:
    """Process-wide limits for the semantic interrupt control plane."""

    enabled: bool = False
    timeout_ms: int = 1000
    max_concurrency: int = 2
    min_confidence: float = 0.80

    def __post_init__(self) -> None:
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if (
            isinstance(self.min_confidence, bool)
            or not isinstance(self.min_confidence, (int, float))
            or not math.isfinite(self.min_confidence)
            or not 0.0 <= self.min_confidence <= 1.0
        ):
            raise ValueError("min_confidence must be finite and between 0 and 1")


@dataclass(frozen=True)
class GatewayTurnArbitrationOutcome:
    """Decision plus prompt-safe controller execution status."""

    status: GatewayTurnArbitrationStatus
    decision: RealtimeTurnArbitrationDecision


class GatewayTurnArbitrationController:
    """Run independent arbitration with a bounded, non-blocking control pool."""

    def __init__(
        self,
        *,
        policy: GatewayTurnArbitrationPolicy | None = None,
        arbiter: RealtimeTurnArbiter | None = None,
        arbiter_factory: Callable[[], RealtimeTurnArbiter] | None = None,
    ) -> None:
        self.policy = policy or GatewayTurnArbitrationPolicy()
        self._arbiter = arbiter
        self._arbiter_factory = arbiter_factory
        self._slots = asyncio.BoundedSemaphore(self.policy.max_concurrency)

    async def decide(
        self,
        request: RealtimeTurnArbitrationRequest,
    ) -> GatewayTurnArbitrationOutcome:
        """Return promptly on saturation while retaining timed-out in-flight slots."""

        if not self.policy.enabled:
            return GatewayTurnArbitrationOutcome(
                status="disabled",
                decision=uncertain_arbitration_decision(
                    request,
                    fallback_reason="semantic_interrupt_disabled",
                ),
            )
        if self._slots.locked():
            return GatewayTurnArbitrationOutcome(
                status="saturated",
                decision=uncertain_arbitration_decision(
                    request,
                    fallback_reason="control_plane_saturated",
                ),
            )

        await self._slots.acquire()
        release_on_return = True
        try:
            try:
                arbiter = self._resolve_arbiter()
                worker = asyncio.create_task(
                    arbiter.arbitrate(request),
                    name=f"realtime-turn-arbiter-{request.decision_id}",
                )
            except Exception:
                return GatewayTurnArbitrationOutcome(
                    status="failed",
                    decision=uncertain_arbitration_decision(
                        request,
                        fallback_reason="arbiter_factory_error",
                    ),
                )

            try:
                decision = await asyncio.wait_for(
                    asyncio.shield(worker),
                    timeout=self.policy.timeout_ms / 1000,
                )
            except asyncio.TimeoutError:
                release_on_return = False
                worker.add_done_callback(self._release_slot_after_worker)
                return GatewayTurnArbitrationOutcome(
                    status="timeout",
                    decision=uncertain_arbitration_decision(
                        request,
                        fallback_reason="arbitration_timeout",
                        latency_ms=self.policy.timeout_ms,
                    ),
                )
            except asyncio.CancelledError:
                release_on_return = False
                worker.add_done_callback(self._release_slot_after_worker)
                raise
            except Exception:
                return GatewayTurnArbitrationOutcome(
                    status="failed",
                    decision=uncertain_arbitration_decision(
                        request,
                        fallback_reason="arbiter_error",
                    ),
                )

            normalized = normalize_arbitration_decision(
                decision.model_dump(mode="json"),
                request=request,
                min_confidence=self.policy.min_confidence,
                source=decision.source,
                latency_ms=decision.latency_ms,
            )
            return GatewayTurnArbitrationOutcome(
                status="completed",
                decision=normalized,
            )
        finally:
            if release_on_return:
                self._slots.release()

    def _resolve_arbiter(self) -> RealtimeTurnArbiter:
        if self._arbiter is None:
            if self._arbiter_factory is None:
                raise RuntimeError("realtime turn arbiter is not configured")
            self._arbiter = self._arbiter_factory()
        return self._arbiter

    def _release_slot_after_worker(
        self,
        worker: asyncio.Task[RealtimeTurnArbitrationDecision],
    ) -> None:
        try:
            worker.exception()
        except (asyncio.CancelledError, Exception):
            pass
        self._slots.release()
