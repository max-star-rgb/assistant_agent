"""Provider call budget and lightweight cost guard."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from assistant_agent.services.identifiers import new_prefixed_uuid7
from assistant_agent.services.provider_errors import ProviderError, build_provider_error


class ProviderCallRecord(BaseModel):
    """One provider-capability call record for a run."""

    run_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    call_count: int = Field(default=1, ge=1)
    estimated_cost: float | None = Field(default=None, ge=0.0)
    cost_unit: str | None = None
    input_size_bytes: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    status: str = Field(min_length=1)


class ProviderCallReservation(BaseModel):
    """One budget slot reserved before a provider-capability invocation starts."""

    reservation_id: str = Field(default_factory=lambda: new_prefixed_uuid7("budget"))
    capability: str = Field(min_length=1)
    provider: str | None = None
    estimated_cost: float | None = Field(default=None, ge=0.0)
    input_size_bytes: int | None = Field(default=None, ge=0)


class ProviderCallBudget(BaseModel):
    """Per-run provider call budget.

    Defaults are conservative and work for offline mock/local execution. This
    object does not enable real providers; it only guards call volume.
    """

    max_provider_calls_per_run: int = Field(default=10, ge=0)
    max_calls_per_capability: dict[str, int] = Field(default_factory=dict)
    max_estimated_cost_per_run: float | None = Field(default=None, ge=0.0)
    max_input_bytes_per_run: int | None = Field(default=None, ge=0)
    allow_real_provider: bool = False
    call_records: list[ProviderCallRecord] = Field(default_factory=list)
    pending_reservations: list[ProviderCallReservation] = Field(
        default_factory=list,
        exclude=True,
    )

    @property
    def provider_call_count(self) -> int:
        """Return provider call count for this run."""

        return sum(record.call_count for record in self.call_records)

    @property
    def estimated_cost_total(self) -> float | None:
        """Return total known estimated cost, or None if all costs are unknown."""

        values = [record.estimated_cost for record in self.call_records if record.estimated_cost is not None]
        if not values:
            return None
        return sum(values)

    def capability_call_count(self, capability: str) -> int:
        """Return call count for one capability."""

        return sum(record.call_count for record in self.call_records if record.capability == capability)

    def check_before_call(
        self,
        *,
        capability: str,
        provider: str | None = None,
        estimated_cost: float | None = None,
        input_size_bytes: int | None = None,
    ) -> ProviderError | None:
        """Return a budget error if the next call should be blocked."""

        if self.provider_call_count + len(self.pending_reservations) >= self.max_provider_calls_per_run:
            return self._error(
                "provider_call_limit_exceeded",
                "Provider call budget exceeded for this run.",
                capability=capability,
                provider=provider,
            )

        capability_limit = self.max_calls_per_capability.get(capability)
        pending_capability_count = sum(
            1 for reservation in self.pending_reservations if reservation.capability == capability
        )
        if (
            capability_limit is not None
            and self.capability_call_count(capability) + pending_capability_count >= capability_limit
        ):
            return self._error(
                "provider_call_limit_exceeded",
                f"Provider call budget exceeded for capability {capability}.",
                capability=capability,
                provider=provider,
            )

        if self.max_estimated_cost_per_run is not None and estimated_cost is not None:
            current_cost = (self.estimated_cost_total or 0.0) + sum(
                reservation.estimated_cost or 0.0
                for reservation in self.pending_reservations
            )
            if current_cost + estimated_cost > self.max_estimated_cost_per_run:
                return self._error(
                    "provider_budget_exceeded",
                    "Provider estimated cost budget exceeded for this run.",
                    capability=capability,
                    provider=provider,
                )

        if self.max_input_bytes_per_run is not None and input_size_bytes is not None:
            current_bytes = sum(record.input_size_bytes or 0 for record in self.call_records) + sum(
                reservation.input_size_bytes or 0
                for reservation in self.pending_reservations
            )
            if current_bytes + input_size_bytes > self.max_input_bytes_per_run:
                return self._error(
                    "provider_input_size_exceeded",
                    "Provider input size budget exceeded for this run.",
                    capability=capability,
                    provider=provider,
                )

        return None

    def reserve_call(
        self,
        *,
        capability: str,
        provider: str | None = None,
        estimated_cost: float | None = None,
        input_size_bytes: int | None = None,
    ) -> tuple[ProviderCallReservation | None, ProviderError | None]:
        """Check and reserve one future call during the coordinator's serial prepare phase."""

        error = self.check_before_call(
            capability=capability,
            provider=provider,
            estimated_cost=estimated_cost,
            input_size_bytes=input_size_bytes,
        )
        if error is not None:
            return None, error
        reservation = ProviderCallReservation(
            capability=capability,
            provider=provider,
            estimated_cost=estimated_cost,
            input_size_bytes=input_size_bytes,
        )
        self.pending_reservations.append(reservation)
        return reservation, None

    def release_reservation(self, reservation_id: str) -> None:
        """Release a pending slot when no provider call result will be committed."""

        self.pending_reservations = [
            item for item in self.pending_reservations if item.reservation_id != reservation_id
        ]

    def record_reserved_call(
        self,
        reservation: ProviderCallReservation,
        *,
        run_id: str,
        provider: str | None = None,
        model: str | None = None,
        estimated_cost: float | None = None,
        use_reserved_estimated_cost: bool = True,
        cost_unit: str | None = None,
        latency_ms: int | None = None,
        status: str,
    ) -> ProviderCallRecord:
        """Consume a reservation and append its final provider call record."""

        self.release_reservation(reservation.reservation_id)
        return self.record_call(
            run_id=run_id,
            capability=reservation.capability,
            provider=provider or reservation.provider,
            model=model,
            estimated_cost=(
                estimated_cost
                if estimated_cost is not None or not use_reserved_estimated_cost
                else reservation.estimated_cost
            ),
            cost_unit=cost_unit,
            input_size_bytes=reservation.input_size_bytes,
            latency_ms=latency_ms,
            status=status,
        )

    def record_call(
        self,
        *,
        run_id: str,
        capability: str,
        provider: str | None = None,
        model: str | None = None,
        estimated_cost: float | None = None,
        cost_unit: str | None = None,
        input_size_bytes: int | None = None,
        latency_ms: int | None = None,
        status: str,
    ) -> ProviderCallRecord:
        """Record one provider-capability call."""

        record = ProviderCallRecord(
            run_id=run_id,
            capability=capability,
            provider=provider,
            model=model,
            estimated_cost=estimated_cost,
            cost_unit=cost_unit,
            input_size_bytes=input_size_bytes,
            latency_ms=latency_ms,
            status=status,
        )
        self.call_records.append(record)
        return record

    def summary(self) -> dict[str, Any]:
        """Return a public budget summary for API/response data."""

        return {
            "provider_call_count": self.provider_call_count,
            "max_provider_calls_per_run": self.max_provider_calls_per_run,
            "calls_by_capability": {
                capability: self.capability_call_count(capability)
                for capability in sorted({record.capability for record in self.call_records})
            },
            "estimated_cost": self.estimated_cost_total,
            "cost_unit": "unknown" if self.estimated_cost_total is None else "estimated",
            "allow_real_provider": self.allow_real_provider,
        }

    def _error(self, code: str, message: str, *, capability: str, provider: str | None) -> ProviderError:
        return build_provider_error(
            code,
            message,
            recoverable=False,
            provider=provider,
            capability=capability,
            detail={"capability": capability, "provider_call_count": self.provider_call_count},
        )
